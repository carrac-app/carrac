"""
Unit tests for Hyperliquid WebSocket worker (data_sources/hyperliquid_ws.py).

All tests use mocked WebSocket connections — no network calls.
"""

import json
import time

import pandas as pd
import pytest

from csv_cache import CSVCache
from data_sources.hyperliquid import HyperliquidClient
from data_sources.hyperliquid_ws import HyperliquidWSWorker


@pytest.fixture
def tmp_cache(tmp_path):
    """Create a temp CSVCache for testing."""
    return CSVCache(cache_dir=str(tmp_path))


@pytest.fixture
def worker(tmp_cache):
    """Create a worker with a real CSVCache but no WS connection.
    Set WRITE_INTERVAL=0 so tests see immediate writes (no throttle)."""
    w = HyperliquidWSWorker(
        csv_cache=tmp_cache,
        hl_coins=HyperliquidClient.HL_COINS,
        k_prefix_translation=HyperliquidClient.K_PREFIX_TRANSLATION,
    )
    w.WRITE_INTERVAL = 0  # Disable throttle for test predictability
    return w


# ─── Sample Messages ────────────────────────────────────

# NOTE: HL WS sends candle data as a dict, not a list — matches real API behavior
HYPE_CANDLE_MSG = json.dumps({
    "channel": "candle",
    "data": {
        "t": 1775703600000, "T": 1775707199999, "s": "HYPE", "i": "1h",
        "o": "38.10", "c": "38.40", "h": "38.55", "l": "38.05", "v": "12345.67", "n": 4321,
    },
})

KPEPE_CANDLE_MSG = json.dumps({
    "channel": "candle",
    "data": {
        "t": 1775703600000, "T": 1775707199999, "s": "kPEPE", "i": "1h",
        "o": "0.0035", "c": "0.0036", "h": "0.0037", "l": "0.0034", "v": "999.99", "n": 100,
    },
})


# ─── Message Parsing ────────────────────────────────────

class TestHandleMessage:
    def test_candle_writes_to_cache(self, worker, tmp_cache):
        worker._handle_message(HYPE_CANDLE_MSG)
        df = tmp_cache.load('HYPE', '1h')
        assert df is not None
        assert len(df) == 1
        assert df['close'].iloc[0] == 38.40
        assert df['volume'].iloc[0] == 12345.67

    def test_kprefix_translated_to_uppercase_storage(self, worker, tmp_cache):
        """kPEPE on the wire should store as KPEPE in cache."""
        worker._handle_message(KPEPE_CANDLE_MSG)
        df = tmp_cache.load('KPEPE', '1h')
        assert df is not None
        assert len(df) == 1
        assert df['close'].iloc[0] == 0.0036

    def test_dedup_keeps_latest(self, worker, tmp_cache):
        """Same timestamp twice — latest wins."""
        worker._handle_message(HYPE_CANDLE_MSG)
        # Second message, same timestamp, different close
        updated = json.dumps({
            "channel": "candle",
            "data": {
                "t": 1775703600000, "T": 1775707199999, "s": "HYPE", "i": "1h",
                "o": "38.10", "c": "39.00", "h": "39.10", "l": "38.05", "v": "15000.0", "n": 5000,
            },
        })
        worker._handle_message(updated)
        df = tmp_cache.load('HYPE', '1h')
        assert len(df) == 1
        assert df['close'].iloc[0] == 39.00

    def test_appends_new_timestamp(self, worker, tmp_cache):
        worker._handle_message(HYPE_CANDLE_MSG)
        # New candle one hour later
        later = json.dumps({
            "channel": "candle",
            "data": {
                "t": 1775707200000, "T": 1775710799999, "s": "HYPE", "i": "1h",
                "o": "38.40", "c": "38.90", "h": "39.00", "l": "38.30", "v": "8000.0", "n": 3000,
            },
        })
        worker._handle_message(later)
        df = tmp_cache.load('HYPE', '1h')
        assert len(df) == 2
        assert df['close'].iloc[-1] == 38.90

    def test_ignores_non_candle_channel(self, worker, tmp_cache):
        msg = json.dumps({"channel": "subscriptionResponse", "data": {"method": "subscribe"}})
        worker._handle_message(msg)
        assert tmp_cache.load('HYPE', '1h') is None

    def test_ignores_unknown_coin(self, worker, tmp_cache):
        msg = json.dumps({
            "channel": "candle",
            "data": {
                "t": 1775703600000, "T": 1775707199999, "s": "UNKNOWN_COIN", "i": "1h",
                "o": "1.0", "c": "1.0", "h": "1.0", "l": "1.0", "v": "1.0", "n": 1,
            },
        })
        worker._handle_message(msg)
        # Nothing stored for unknown
        assert tmp_cache.load('UNKNOWN_COIN', '1h') is None

    def test_ignores_zero_close(self, worker, tmp_cache):
        msg = json.dumps({
            "channel": "candle",
            "data": {
                "t": 1775703600000, "T": 1775707199999, "s": "HYPE", "i": "1h",
                "o": "0", "c": "0", "h": "0", "l": "0", "v": "0", "n": 0,
            },
        })
        worker._handle_message(msg)
        assert tmp_cache.load('HYPE', '1h') is None

    def test_updates_last_message_ts(self, worker):
        before = time.time()
        worker._handle_message(HYPE_CANDLE_MSG)
        after = time.time()
        ts = worker._last_message_ts.get('HYPE')
        assert ts is not None
        assert before <= ts <= after

    def test_invalid_json_ignored(self, worker, tmp_cache):
        worker._handle_message("not json at all")
        assert tmp_cache.load('HYPE', '1h') is None


# ─── Healthcheck ────────────────────────────────────────

class TestHealthcheck:
    def test_all_stale_initially(self, worker):
        hc = worker.healthcheck()
        assert hc['enabled'] is True
        assert hc['connected'] is False
        assert len(hc['stale_coins']) == len(HyperliquidClient.HL_COINS)

    def test_coin_fresh_after_message(self, worker):
        worker._handle_message(HYPE_CANDLE_MSG)
        hc = worker.healthcheck()
        assert 'HYPE' not in hc['stale_coins']

    def test_stale_threshold(self, worker):
        worker._last_message_ts['HYPE'] = time.time() - 600  # 10 min ago
        hc = worker.healthcheck()
        assert 'HYPE' in hc['stale_coins']

    def test_thread_alive_flag(self, worker):
        hc = worker.healthcheck()
        assert hc['thread_alive'] is False  # not started yet


# ─── Kill Switch ────────────────────────────────────────

class TestKillSwitch:
    def test_start_aborts_when_disabled(self, worker, monkeypatch):
        monkeypatch.setenv('HYPERLIQUID_DISABLED', '1')
        worker.start()
        assert worker._thread is None  # never spawned

    def test_start_works_when_not_disabled(self, worker, monkeypatch):
        """Worker.start() spawns a thread when not disabled.
        We mock _run_forever to avoid actual WS connection."""
        monkeypatch.delenv('HYPERLIQUID_DISABLED', raising=False)
        worker._run_forever = lambda: worker._stop_event.wait()  # block until stop
        worker.start()
        assert worker._thread is not None
        assert worker._thread.is_alive()
        worker.stop()


# ─── Cache Trimming ─────────────────────────────────────

class TestCacheTrimming:
    def test_cache_trimmed_to_max_rows(self, worker, tmp_cache):
        # Pre-seed cache with MAX_CACHE_ROWS rows
        original_max = worker.MAX_CACHE_ROWS
        worker.MAX_CACHE_ROWS = 5  # small for test
        dates = pd.date_range('2026-01-01', periods=5, freq='h')
        df = pd.DataFrame({
            'open': [1.0] * 5, 'high': [1.0] * 5, 'low': [1.0] * 5,
            'close': [1.0] * 5, 'volume': [1.0] * 5,
        }, index=dates)
        tmp_cache.save('HYPE', df, '1h')

        # Now add one more candle — should trim to 5
        msg = json.dumps({
            "channel": "candle",
            "data": [{
                "t": int(pd.Timestamp('2026-01-01 05:00').timestamp() * 1000),
                "T": int(pd.Timestamp('2026-01-01 06:00').timestamp() * 1000),
                "s": "HYPE", "i": "1h",
                "o": "2.0", "c": "2.0", "h": "2.0", "l": "2.0", "v": "100.0", "n": 10,
            }],
        })
        worker._handle_message(msg)
        df_out = tmp_cache.load('HYPE', '1h')
        assert len(df_out) == 5
        assert df_out['close'].iloc[-1] == 2.0
        worker.MAX_CACHE_ROWS = original_max  # restore


# ─── HL_COINS Invariant ────────────────────────────────

class TestWorkerCoinMapping:
    def test_reverse_k_prefix_map(self, worker):
        """kPEPE on the wire → KPEPE internally."""
        assert worker._k_reverse.get('kPEPE') == 'KPEPE'
        assert worker._k_reverse.get('kBONK') == 'KBONK'

    def test_all_hl_coins_have_forward_translation(self, worker):
        """Every coin in HL_COINS that starts with K should have a translation."""
        for coin in HyperliquidClient.HL_COINS:
            if coin.startswith('K') and len(coin) > 1 and coin[1:].isupper():
                api_name = worker._k_translate.get(coin)
                assert api_name is not None, f"{coin} needs K_PREFIX_TRANSLATION entry"
                assert api_name.startswith('k'), f"{coin} translation should start with 'k'"
