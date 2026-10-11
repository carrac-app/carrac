"""
Unit tests for the Binance USDT-futures forced-liquidation WS consumer
(data_sources/binance_liq_ws.py).

All tests use fakes / mocks -- no live network, no live Redis, no live DB.
"""

import json
import time
from collections import deque
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from data_sources.binance_liq_ws import BinanceLiqWSWorker


TEST_SYMBOL_MAP = {
    'BTCUSDT': 'BTC',
    'ETHUSDT': 'ETH',
}


def _force_order_msg(symbol='BTCUSDT', side='SELL', qty='0.5', avg_price='60000',
                      trade_time_ms=None):
    """Build a raw !forceOrder@arr message string (Binance wire format).

    Defaults trade_time_ms to "now" -- _handle_message prunes against the
    real wall clock (time.time()) on every call, so a fixed historical
    constant would get silently evicted by the 60-minute window before a
    test could assert anything about it.
    """
    if trade_time_ms is None:
        trade_time_ms = int(time.time() * 1000)
    return json.dumps({
        "e": "forceOrder",
        "E": trade_time_ms,
        "o": {
            "s": symbol, "S": side, "o": "LIMIT", "f": "IOC",
            "q": qty, "p": avg_price, "ap": avg_price, "X": "FILLED",
            "l": qty, "z": qty, "T": trade_time_ms,
        },
    })


@pytest.fixture
def worker():
    return BinanceLiqWSWorker(symbol_map=dict(TEST_SYMBOL_MAP))


# ─── Message Parsing ────────────────────────────────────

class TestMessageParsing:
    def test_sell_is_long_liquidation(self, worker):
        """S=SELL means a LONG was liquidated -> long bucket."""
        worker._handle_message(_force_order_msg(side='SELL', qty='0.5', avg_price='60000'))
        dq = worker._events['BTC']
        assert len(dq) == 1
        ts, bucket, notional = dq[0]
        assert bucket == 'long'
        assert notional == pytest.approx(0.5 * 60000)

    def test_buy_is_short_liquidation(self, worker):
        """S=BUY means a SHORT was liquidated -> short bucket."""
        worker._handle_message(_force_order_msg(symbol='ETHUSDT', side='BUY', qty='2', avg_price='3000'))
        dq = worker._events['ETH']
        assert len(dq) == 1
        ts, bucket, notional = dq[0]
        assert bucket == 'short'
        assert notional == pytest.approx(6000.0)

    def test_notional_is_qty_times_avg_price(self, worker):
        worker._handle_message(_force_order_msg(qty='1.25', avg_price='40000'))
        _, _, notional = worker._events['BTC'][0]
        assert notional == pytest.approx(1.25 * 40000)

    def test_ts_converted_ms_to_seconds(self, worker):
        trade_time_ms = int(time.time() * 1000) - 5000  # 5s ago -- well within window
        worker._handle_message(_force_order_msg(trade_time_ms=trade_time_ms))
        ts, _, _ = worker._events['BTC'][0]
        assert ts == pytest.approx(trade_time_ms / 1000.0)

    def test_unmapped_symbol_dropped(self, worker):
        worker._handle_message(_force_order_msg(symbol='DOGEUSDT'))
        assert not worker._events  # nothing stored -- DOGE isn't in the injected map

    def test_invalid_json_ignored(self, worker):
        worker._handle_message("not json at all")
        assert not worker._events

    def test_non_dict_json_ignored(self, worker):
        worker._handle_message(json.dumps([1, 2, 3]))
        assert not worker._events

    def test_missing_o_key_ignored(self, worker):
        worker._handle_message(json.dumps({"e": "forceOrder", "E": 123}))
        assert not worker._events

    def test_bad_side_ignored(self, worker):
        worker._handle_message(_force_order_msg(side='HOLD'))
        assert not worker._events

    def test_non_numeric_qty_ignored(self, worker):
        msg = json.dumps({
            "e": "forceOrder", "E": 1,
            "o": {"s": "BTCUSDT", "S": "SELL", "q": "abc", "ap": "60000", "T": 1},
        })
        worker._handle_message(msg)
        assert not worker._events

    def test_missing_field_ignored(self, worker):
        msg = json.dumps({
            "e": "forceOrder", "E": 1,
            "o": {"s": "BTCUSDT", "S": "SELL", "ap": "60000", "T": 1},  # no q
        })
        worker._handle_message(msg)
        assert not worker._events

    def test_zero_notional_ignored(self, worker):
        worker._handle_message(_force_order_msg(qty='0', avg_price='60000'))
        assert not worker._events

    def test_updates_last_message_ts(self, worker):
        before = time.time()
        worker._handle_message(_force_order_msg())
        after = time.time()
        ts = worker._last_message_ts.get('BTC')
        assert ts is not None
        assert before <= ts <= after

    def test_event_count_incremented(self, worker):
        worker._handle_message(_force_order_msg())
        worker._handle_message(_force_order_msg(symbol='ETHUSDT', side='BUY'))
        assert worker._event_count_since_log == 2

    def test_malformed_message_does_not_increment_count(self, worker):
        worker._handle_message("garbage")
        assert worker._event_count_since_log == 0


# ─── Window Pruning ─────────────────────────────────────

class TestWindowPruning:
    def test_prune_removes_events_older_than_60min(self, worker):
        now = time.time()
        worker._events['BTC'] = deque([
            (now - 4000, 'long', 100.0),   # > 60 min old -- pruned
            (now - 100, 'long', 200.0),    # fresh -- kept
        ], maxlen=worker.MAX_EVENTS_PER_SYMBOL)
        worker._prune('BTC', now)
        assert list(worker._events['BTC']) == [(now - 100, 'long', 200.0)]

    def test_prune_keeps_events_within_window(self, worker):
        now = time.time()
        worker._events['BTC'] = deque([
            (now - 3599, 'long', 1.0),
            (now - 10, 'short', 2.0),
        ], maxlen=worker.MAX_EVENTS_PER_SYMBOL)
        worker._prune('BTC', now)
        assert len(worker._events['BTC']) == 2

    def test_prune_noop_for_untracked_symbol(self, worker):
        worker._prune('NOPE', time.time())  # must not raise

    def test_max_events_per_symbol_cap_via_deque_maxlen(self, worker):
        small = 3
        worker._events['BTC'] = deque(maxlen=small)
        for i in range(10):
            worker._events['BTC'].append((time.time(), 'long', float(i)))
        assert len(worker._events['BTC']) == small

    def test_handle_message_creates_capped_deque(self, worker):
        worker.MAX_EVENTS_PER_SYMBOL = 2
        for _ in range(5):
            worker._handle_message(_force_order_msg(trade_time_ms=int(time.time() * 1000)))
        assert len(worker._events['BTC']) == 2


# ─── Summary JSON Contract ──────────────────────────────

class TestSummaryContract:
    def test_summary_matches_contract_shape_round_trip(self, worker):
        # Derive `now` FROM the millisecond timestamp we feed into the events
        # (rather than the reverse) so last_ts / updated_at match exactly --
        # no float-precision flakiness, and no risk of the 60-min prune
        # (which runs against the real wall clock) evicting a stale fixture.
        now_ms = int(time.time() * 1000)
        now = now_ms / 1000.0
        worker._handle_message(_force_order_msg(
            symbol='BTCUSDT', side='SELL', qty='1', avg_price='50000',
            trade_time_ms=now_ms))
        worker._handle_message(_force_order_msg(
            symbol='ETHUSDT', side='BUY', qty='2', avg_price='3000',
            trade_time_ms=now_ms))

        summary = worker._compute_summary(now)
        raw = json.dumps(summary)
        round_tripped = json.loads(raw)  # round-trip through the exact wire format

        assert set(round_tripped.keys()) == {'updated_at', 'symbols'}
        assert round_tripped['updated_at'] == int(now)
        assert isinstance(round_tripped['updated_at'], int)

        assert round_tripped['symbols']['BTC'] == {
            'm15_long_usd': 50000.0,
            'm15_short_usd': 0.0,
            'h1_long_usd': 50000.0,
            'h1_short_usd': 0.0,
            'last_ts': int(now),
        }
        assert round_tripped['symbols']['ETH'] == {
            'm15_long_usd': 0.0,
            'm15_short_usd': 6000.0,
            'h1_long_usd': 0.0,
            'h1_short_usd': 6000.0,
            'last_ts': int(now),
        }

    def test_m15_excludes_events_older_than_15min_but_within_1h(self, worker):
        now = time.time()
        old_ts_ms = int((now - 1200) * 1000)  # 20 min ago -- inside h1, outside m15
        worker._handle_message(_force_order_msg(trade_time_ms=old_ts_ms))
        summary = worker._compute_summary(now)
        btc = summary['symbols']['BTC']
        assert btc['m15_long_usd'] == 0.0
        assert btc['h1_long_usd'] == pytest.approx(0.5 * 60000)

    def test_symbol_drops_out_after_full_window_expiry(self, worker):
        now = time.time()
        worker._events['BTC'] = deque([(now - 4000, 'long', 1.0)], maxlen=worker.MAX_EVENTS_PER_SYMBOL)
        summary = worker._compute_summary(now)
        assert 'BTC' not in summary['symbols']

    def test_heartbeat_updated_at_always_present_even_when_quiet(self, worker):
        now = time.time()
        summary = worker._compute_summary(now)
        assert summary == {'updated_at': int(now), 'symbols': {}}

    def test_last_ts_is_max_event_ts_in_window(self, worker):
        now = time.time()
        worker._handle_message(_force_order_msg(trade_time_ms=int((now - 500) * 1000)))
        worker._handle_message(_force_order_msg(trade_time_ms=int((now - 10) * 1000)))
        summary = worker._compute_summary(now)
        assert summary['symbols']['BTC']['last_ts'] == int(now - 10)


# ─── Symbol Mapping ─────────────────────────────────────

class TestSymbolMapping:
    def test_default_symbol_map_derived_from_binance_perp_map(self):
        w = BinanceLiqWSWorker()  # no injected map -> uses BinanceClient.PERP_MAP reversed
        from data_sources.binance import BinanceClient
        assert w._symbol_map['BTCUSDT'] == 'BTC'
        assert w._symbol_map == {v: k for k, v in BinanceClient.PERP_MAP.items()}

    def test_unmapped_perp_symbol_is_dropped_end_to_end(self, worker):
        worker._handle_message(_force_order_msg(symbol='SOMECOINUSDT'))
        summary = worker._compute_summary(time.time())
        assert summary['symbols'] == {}


# ─── Daily quant.derivatives Upsert ─────────────────────

class TestDailyUpsert:
    def test_accumulate_daily_tracks_target_asset(self, worker):
        ts_ms = int(datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc).timestamp() * 1000)
        worker._handle_message(_force_order_msg(symbol='BTCUSDT', side='SELL',
                                                 qty='1', avg_price='50000', trade_time_ms=ts_ms))
        assert worker._daily_totals[('2026-07-06', 'BTC')]['liquidations_long'] == pytest.approx(50000.0)
        assert worker._daily_totals[('2026-07-06', 'BTC')]['liquidations_short'] == 0.0

    def test_accumulate_daily_skips_non_target_asset(self):
        w = BinanceLiqWSWorker(symbol_map={'FOOUSDT': 'FOO'})
        w._handle_message(_force_order_msg(symbol='FOOUSDT', side='SELL', qty='1', avg_price='100'))
        assert not w._daily_totals  # FOO not in TARGET_ASSETS -- never tracked for DB persistence
        assert w._events  # but it IS still tracked for the redis summary window

    def test_accumulate_daily_sums_both_sides(self, worker):
        ts_ms = int(datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc).timestamp() * 1000)
        worker._handle_message(_force_order_msg(symbol='BTCUSDT', side='SELL',
                                                 qty='1', avg_price='50000', trade_time_ms=ts_ms))
        worker._handle_message(_force_order_msg(symbol='BTCUSDT', side='BUY',
                                                 qty='2', avg_price='3000', trade_time_ms=ts_ms))
        totals = worker._daily_totals[('2026-07-06', 'BTC')]
        assert totals['liquidations_long'] == pytest.approx(50000.0)
        assert totals['liquidations_short'] == pytest.approx(6000.0)

    @patch('carrac.db_pool.get_quant_conn')
    def test_upsert_daily_totals_calls_db_with_accumulated_values(self, mock_get_conn, worker):
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value = mock_cur
        mock_get_conn.return_value.__enter__.return_value = mock_conn

        ts_ms = int(datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc).timestamp() * 1000)
        worker._handle_message(_force_order_msg(symbol='BTCUSDT', side='SELL',
                                                 qty='1', avg_price='50000', trade_time_ms=ts_ms))
        worker._handle_message(_force_order_msg(symbol='BTCUSDT', side='BUY',
                                                 qty='2', avg_price='3000', trade_time_ms=ts_ms))

        worker._upsert_daily_totals()

        assert mock_cur.execute.called
        sql, params = mock_cur.execute.call_args[0]
        assert 'quant.derivatives' in sql
        assert 'ON CONFLICT (ts, asset) DO UPDATE' in sql
        assert params == ('2026-07-06', 'BTC', pytest.approx(50000.0), pytest.approx(6000.0))

    def test_upsert_daily_totals_noop_when_nothing_accumulated(self, worker):
        with patch('carrac.db_pool.get_quant_conn') as mock_get_conn:
            worker._upsert_daily_totals()
            mock_get_conn.assert_not_called()

    @patch('carrac.db_pool.get_quant_conn')
    def test_upsert_prunes_stale_dates_from_memory(self, mock_get_conn, worker):
        mock_conn = MagicMock()
        mock_get_conn.return_value.__enter__.return_value = mock_conn
        worker._daily_totals[('2020-01-01', 'BTC')] = {'liquidations_long': 1.0, 'liquidations_short': 0.0}
        worker._upsert_daily_totals()
        assert ('2020-01-01', 'BTC') not in worker._daily_totals

    def test_stop_flushes_daily_totals(self, worker):
        """stop() should flush the daily upsert without raising even with no DB configured."""
        worker._daily_totals[('2026-07-06', 'BTC')] = {'liquidations_long': 1.0, 'liquidations_short': 0.0}
        with patch('carrac.db_pool.get_quant_conn') as mock_get_conn:
            mock_conn = MagicMock()
            mock_get_conn.return_value.__enter__.return_value = mock_conn
            worker.stop()  # threads were never started -- should still flush cleanly
            mock_get_conn.assert_called_once()


# ─── Per-Event Ring Buffer ────────────────────────────────

class TestEventPersistence:
    def test_handle_message_buffers_target_asset(self, worker):
        ts_ms = int(datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc).timestamp() * 1000)
        worker._handle_message(_force_order_msg(symbol='BTCUSDT', side='SELL',
                                                 qty='1', avg_price='50000', trade_time_ms=ts_ms))
        assert len(worker._pending_events) == 1
        row = worker._pending_events[0]
        ts_dt, asset, side, price, qty, notional = row
        assert asset == 'BTC'
        assert side == 'long'
        assert price == pytest.approx(50000.0)
        assert qty == pytest.approx(1.0)
        assert notional == pytest.approx(50000.0)
        assert ts_dt.tzinfo is not None

    def test_buffer_event_skips_non_target_asset(self):
        w = BinanceLiqWSWorker(symbol_map={'FOOUSDT': 'FOO'})
        w._handle_message(_force_order_msg(symbol='FOOUSDT', side='SELL', qty='1', avg_price='100'))
        assert not w._pending_events  # FOO not in TARGET_ASSETS

    def test_buffer_event_capped_at_max_pending(self, worker):
        worker.MAX_PENDING_EVENTS = 3
        ts_ms = int(datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc).timestamp() * 1000)
        for _ in range(10):
            worker._handle_message(_force_order_msg(symbol='BTCUSDT', side='SELL',
                                                      qty='1', avg_price='50000', trade_time_ms=ts_ms))
        assert len(worker._pending_events) == 3  # excess dropped, not grown unbounded

    @patch('carrac.db_pool.get_quant_conn')
    def test_flush_events_to_db_executemany_and_clears_buffer(self, mock_get_conn, worker):
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value = mock_cur
        mock_get_conn.return_value.__enter__.return_value = mock_conn

        ts_ms = int(datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc).timestamp() * 1000)
        worker._handle_message(_force_order_msg(symbol='BTCUSDT', side='SELL',
                                                 qty='1', avg_price='50000', trade_time_ms=ts_ms))
        worker._handle_message(_force_order_msg(symbol='BTCUSDT', side='BUY',
                                                 qty='2', avg_price='3000', trade_time_ms=ts_ms))

        worker._flush_events_to_db()

        assert mock_cur.executemany.called
        sql, batch = mock_cur.executemany.call_args[0]
        assert 'quant.liquidation_events' in sql
        assert len(batch) == 2
        assert not worker._pending_events  # buffer cleared after a successful flush

    def test_flush_events_to_db_noop_when_empty(self, worker):
        with patch('carrac.db_pool.get_quant_conn') as mock_get_conn:
            worker._flush_events_to_db()
            mock_get_conn.assert_not_called()

    @patch('carrac.db_pool.get_quant_conn')
    def test_flush_events_to_db_requeues_on_failure(self, mock_get_conn, worker):
        mock_get_conn.side_effect = RuntimeError("db down")

        ts_ms = int(datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc).timestamp() * 1000)
        worker._handle_message(_force_order_msg(symbol='BTCUSDT', side='SELL',
                                                 qty='1', avg_price='50000', trade_time_ms=ts_ms))
        worker._flush_events_to_db()  # must not raise
        assert len(worker._pending_events) == 1  # re-queued, not lost

    @patch('carrac.db_pool.get_quant_conn')
    def test_prune_old_events_deletes_by_retention(self, mock_get_conn, worker):
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value = mock_cur
        mock_get_conn.return_value.__enter__.return_value = mock_conn

        worker._prune_old_events()

        assert mock_cur.execute.called
        sql, params = mock_cur.execute.call_args[0]
        assert 'DELETE FROM quant.liquidation_events' in sql
        assert params == (worker.EVENTS_RETENTION_DAYS,)

    def test_stop_flushes_pending_events(self, worker):
        ts_ms = int(datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc).timestamp() * 1000)
        worker._handle_message(_force_order_msg(symbol='BTCUSDT', side='SELL',
                                                 qty='1', avg_price='50000', trade_time_ms=ts_ms))
        with patch('carrac.db_pool.get_quant_conn') as mock_get_conn:
            mock_conn = MagicMock()
            mock_cur = MagicMock()
            mock_conn.cursor.return_value = mock_cur
            mock_get_conn.return_value.__enter__.return_value = mock_conn
            worker.stop()
            assert mock_cur.executemany.called  # final events flush happened

    def test_healthcheck_reports_pending_events(self, worker):
        ts_ms = int(datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc).timestamp() * 1000)
        worker._handle_message(_force_order_msg(symbol='BTCUSDT', side='SELL',
                                                 qty='1', avg_price='50000', trade_time_ms=ts_ms))
        assert worker.healthcheck()['pending_events_buffered'] == 1


# ─── Redis Summary Write ────────────────────────────────

class TestRedisSummaryWrite:
    @patch('carrac.redis_cache.get_redis')
    def test_write_summary_sets_expected_key_and_ttl(self, mock_get_redis, worker):
        mock_r = MagicMock()
        mock_get_redis.return_value = mock_r
        now_ms = int(time.time() * 1000)
        now = now_ms / 1000.0
        worker._handle_message(_force_order_msg(trade_time_ms=now_ms))

        worker._write_summary(now)

        mock_r.set.assert_called_once()
        args, kwargs = mock_r.set.call_args
        assert args[0] == 'mcd:liq:summary'
        payload = json.loads(args[1])
        assert payload['updated_at'] == int(now)
        assert 'BTC' in payload['symbols']
        assert kwargs.get('ex') == worker.REDIS_TTL

    @patch('carrac.redis_cache.get_redis')
    def test_write_summary_noop_when_redis_unavailable(self, mock_get_redis, worker):
        mock_get_redis.return_value = None
        worker._write_summary(time.time())  # must not raise

    @patch('carrac.redis_cache.get_redis')
    def test_write_summary_swallows_redis_errors(self, mock_get_redis, worker):
        mock_r = MagicMock()
        mock_r.set.side_effect = Exception("boom")
        mock_get_redis.return_value = mock_r
        worker._write_summary(time.time())  # must not raise


# ─── Reconnect / Backoff ────────────────────────────────

class TestBackoff:
    def test_backoff_grows_exponentially(self, worker):
        worker._reconnect_count = 0
        assert worker._backoff_seconds() == 1
        worker._reconnect_count = 1
        assert worker._backoff_seconds() == 2
        worker._reconnect_count = 3
        assert worker._backoff_seconds() == 8

    def test_backoff_caps_at_max(self, worker):
        worker._reconnect_count = 10
        assert worker._backoff_seconds() == worker.MAX_BACKOFF_SECONDS

    def test_connect_and_run_handles_missing_websocket_client(self, worker):
        """If websocket-client somehow isn't importable, degrade gracefully
        (stop, don't crash the worker)."""
        import builtins
        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == 'websocket':
                raise ImportError("no websocket-client")
            return real_import(name, *args, **kwargs)

        with patch('builtins.__import__', side_effect=fake_import):
            worker._connect_and_run()
        assert worker._stop_event.is_set()


# ─── Lifecycle / Kill Switch ─────────────────────────────

class TestLifecycle:
    def test_start_spawns_both_threads(self, worker):
        worker._run_forever = lambda: worker._stop_event.wait()
        worker._summary_loop = lambda: worker._stop_event.wait()
        worker.start()
        assert worker._thread is not None and worker._thread.is_alive()
        assert worker._summary_thread is not None and worker._summary_thread.is_alive()
        worker.stop()
        assert not worker._thread.is_alive()
        assert not worker._summary_thread.is_alive()

    def test_start_idempotent(self, worker):
        worker._run_forever = lambda: worker._stop_event.wait()
        worker._summary_loop = lambda: worker._stop_event.wait()
        worker.start()
        first_thread = worker._thread
        worker.start()  # second call should be a no-op (already running)
        assert worker._thread is first_thread
        worker.stop()

    def test_healthcheck_before_start(self, worker):
        hc = worker.healthcheck()
        assert hc['enabled'] is True
        assert hc['connected'] is False
        assert hc['thread_alive'] is False
        assert hc['summary_thread_alive'] is False
