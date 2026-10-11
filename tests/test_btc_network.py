"""
Tests for quant.collectors.btc_network — pure math, no real network/DB.

TDD: written BEFORE the implementation, following tests/test_stablecoin_onchain.py style.
All network (requests.get) and DB (carrac.db_pool.get_quant_conn) calls are monkeypatched — no
external dependencies.

Coverage contract (per build spec):
  - string→int satoshi parsing (totalReward / totalFee are JSON strings)
  - /1e8 unit conversion: int(str_satoshis) / 1e8 = BTC amount
  - /1e18 unit conversion: currentHashrate / 1e18 = EH/s
  - miner_rev USD = reward_btc * btc_close
  - tx_fees USD = fee_btc * btc_close
  - difficulty_adj % = (adj - 1.0) * 100
  - no-close skip: USD metrics omitted, non-USD metrics still written
  - per-endpoint partial-failure isolation (one bad endpoint never aborts the run)
  - BTC_BLOCK_HEIGHT written from bare-integer response
  - latest adjustment used (last entry in difficulty list)
"""

import pytest
from unittest.mock import patch, MagicMock

from quant.collectors.btc_network import (
    BTCNetworkCollector,
    REWARD_STATS_URL,
    HASHRATE_URL,
    MEMPOOL_URL,
    HEIGHT_URL,
    USER_AGENT,
)


# ─────────────────────────────────────────────────────────────────────────────
# Test helpers
# ─────────────────────────────────────────────────────────────────────────────

def _mock_get(url_resp_map: dict):
    """
    Return a requests.get side_effect that dispatches by URL.

    url_resp_map values:
      - a MagicMock response → returned as-is
      - an Exception instance → raised
    """
    def _side_effect(url, **kwargs):
        if url not in url_resp_map:
            raise ValueError(f"Unexpected URL in test: {url!r}")
        val = url_resp_map[url]
        if isinstance(val, Exception):
            raise val
        return val
    return _side_effect


def _json_resp(payload: dict) -> MagicMock:
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = payload
    resp.text = ''
    return resp


def _text_resp(text: str) -> MagicMock:
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.text = text
    resp.json.side_effect = ValueError('not JSON')
    return resp


def _reward_stats_payload(total_reward='1500000000', total_fee='50000000', total_tx=5000):
    """Default reward-stats/144 payload. Values are STRINGS per the API contract."""
    return {'totalReward': total_reward, 'totalFee': total_fee, 'totalTx': total_tx}


def _hashrate_payload(hashrate=9.7e20, difficulty_adj=1.0796):
    """Default hashrate/1y payload with two difficulty epochs."""
    return {
        'currentHashrate': hashrate,
        'currentDifficulty': 100_000_000_000_000,
        'difficulty': [
            {'time': 1719000000, 'height': 850000, 'difficulty': 90_000_000_000_000, 'adjustment': 1.0},
            {'time': 1720000000, 'height': 852016, 'difficulty': 100_000_000_000_000, 'adjustment': difficulty_adj},
        ],
    }


def _mempool_payload(vsize=25_000_000):
    return {'count': 10000, 'vsize': vsize, 'total_fee': 5000000, 'fee_histogram': []}


def _all_endpoints_ok():
    """Return a full url→resp dict for a successful run of all four endpoints."""
    return {
        REWARD_STATS_URL: _json_resp(_reward_stats_payload()),
        HASHRATE_URL:     _json_resp(_hashrate_payload()),
        MEMPOOL_URL:      _json_resp(_mempool_payload()),
        HEIGHT_URL:       _text_resp('956086'),
    }


def _mock_db_close(close: float) -> MagicMock:
    """Return a mock get_conn context manager yielding `close` from cursor."""
    row = {'close': close}
    mock_cur = MagicMock()
    mock_cur.fetchone.return_value = row
    mock_conn = MagicMock()
    mock_conn.__enter__.return_value = mock_conn
    mock_conn.__exit__.return_value = False
    mock_conn.cursor.return_value = mock_cur
    return mock_conn


def _mock_db_no_close() -> MagicMock:
    """Return a mock get_conn where fetchone returns None (no BTC close row)."""
    mock_cur = MagicMock()
    mock_cur.fetchone.return_value = None
    mock_conn = MagicMock()
    mock_conn.__enter__.return_value = mock_conn
    mock_conn.__exit__.return_value = False
    mock_conn.cursor.return_value = mock_cur
    return mock_conn


# ─────────────────────────────────────────────────────────────────────────────
# /1e18 hashrate conversion
# ─────────────────────────────────────────────────────────────────────────────

class TestHashrateConversion:
    """currentHashrate is raw H/s; dividing by 1e18 gives EH/s."""

    def test_collector_stores_ehs(self):
        """Collector writes BTC_HASHRATE in EH/s, not raw H/s."""
        c = BTCNetworkCollector(dry_run=True)
        hashrate_raw = 9.7e20
        url_map = _all_endpoints_ok()
        url_map[HASHRATE_URL] = _json_resp(_hashrate_payload(hashrate=hashrate_raw))

        with patch('requests.get', side_effect=_mock_get(url_map)), \
             patch('quant.collectors.btc_network.get_conn', return_value=_mock_db_close(60_000.0)), \
             patch.object(c, 'upsert_rows') as mock_upsert:
            c.collect()

        rows = mock_upsert.call_args.args[1]
        hashrate_row = next(r for r in rows if r['metric'] == 'BTC_HASHRATE')
        assert hashrate_row['value'] == pytest.approx(hashrate_raw / 1e18, rel=1e-4)


# ─────────────────────────────────────────────────────────────────────────────
# Miner revenue USD math
# ─────────────────────────────────────────────────────────────────────────────

class TestMinerRevenueUSD:
    """BTC_MINER_REVENUE_USD = int(totalReward) / 1e8 * btc_close."""

    def test_collector_writes_revenue_usd(self):
        """Collector writes BTC_MINER_REVENUE_USD with correct value and source."""
        total_reward_sat = '1500000000'
        btc_close = 60_000.0

        c = BTCNetworkCollector(dry_run=True)
        url_map = _all_endpoints_ok()
        url_map[REWARD_STATS_URL] = _json_resp(_reward_stats_payload(total_reward=total_reward_sat))

        with patch('requests.get', side_effect=_mock_get(url_map)), \
             patch('quant.collectors.btc_network.get_conn', return_value=_mock_db_close(btc_close)), \
             patch.object(c, 'upsert_rows') as mock_upsert:
            c.collect()

        rows = mock_upsert.call_args.args[1]
        rev_row = next(r for r in rows if r['metric'] == 'BTC_MINER_REVENUE_USD')
        assert rev_row['value'] == pytest.approx(int(total_reward_sat) / 1e8 * btc_close, rel=1e-4)
        assert rev_row['source'] == 'mempool+local'
        assert rev_row['asset'] == 'BTC'

    def test_total_reward_includes_subsidy_and_fees(self):
        """totalReward already = subsidy + fees; no double-counting needed."""
        subsidy_sat = 312_500_000  # 3.125 BTC
        fee_sat = 75_000_000       # 0.75 BTC
        total = str(subsidy_sat + fee_sat)
        expected = (subsidy_sat + fee_sat) / 1e8 * 50_000.0
        assert int(total) / 1e8 * 50_000.0 == pytest.approx(expected)


# ─────────────────────────────────────────────────────────────────────────────
# TX fees USD math
# ─────────────────────────────────────────────────────────────────────────────

class TestTxFeesUSD:
    """BTC_TX_FEES_USD = int(totalFee) / 1e8 * btc_close."""

    def test_collector_writes_fees_usd(self):
        """Collector writes BTC_TX_FEES_USD with correct value and source."""
        total_fee_sat = '50000000'
        btc_close = 60_000.0

        c = BTCNetworkCollector(dry_run=True)
        url_map = _all_endpoints_ok()
        url_map[REWARD_STATS_URL] = _json_resp(_reward_stats_payload(total_fee=total_fee_sat))

        with patch('requests.get', side_effect=_mock_get(url_map)), \
             patch('quant.collectors.btc_network.get_conn', return_value=_mock_db_close(btc_close)), \
             patch.object(c, 'upsert_rows') as mock_upsert:
            c.collect()

        rows = mock_upsert.call_args.args[1]
        fee_row = next(r for r in rows if r['metric'] == 'BTC_TX_FEES_USD')
        assert fee_row['value'] == pytest.approx(int(total_fee_sat) / 1e8 * btc_close, rel=1e-4)
        assert fee_row['source'] == 'mempool+local'


# ─────────────────────────────────────────────────────────────────────────────
# Difficulty adjustment percent
# ─────────────────────────────────────────────────────────────────────────────

class TestDifficultyAdjPercent:
    """BTC_DIFFICULTY_ADJ = (latest_adjustment - 1.0) * 100 (percent)."""

    def test_collector_writes_adj_percent(self):
        """Collector writes BTC_DIFFICULTY_ADJ as (adj-1)*100 percent."""
        adj = 1.0796
        c = BTCNetworkCollector(dry_run=True)
        url_map = _all_endpoints_ok()
        url_map[HASHRATE_URL] = _json_resp(_hashrate_payload(difficulty_adj=adj))

        with patch('requests.get', side_effect=_mock_get(url_map)), \
             patch('quant.collectors.btc_network.get_conn', return_value=_mock_db_close(60_000.0)), \
             patch.object(c, 'upsert_rows') as mock_upsert:
            c.collect()

        rows = mock_upsert.call_args.args[1]
        adj_row = next(r for r in rows if r['metric'] == 'BTC_DIFFICULTY_ADJ')
        assert adj_row['value'] == pytest.approx((adj - 1.0) * 100, rel=1e-4)
        assert adj_row['source'] == 'mempool.space'

    def test_latest_difficulty_entry_used(self):
        """The LAST entry in the difficulty array is used (most recent epoch)."""
        c = BTCNetworkCollector(dry_run=True)
        # Three entries: old=+1%, middle=+3%, latest=+8%
        payload = {
            'currentHashrate': 9.7e20,
            'currentDifficulty': 100_000_000_000_000,
            'difficulty': [
                {'time': 1700000000, 'height': 800000, 'difficulty': 90e12, 'adjustment': 1.01},
                {'time': 1710000000, 'height': 840000, 'difficulty': 95e12, 'adjustment': 1.03},
                {'time': 1720000000, 'height': 852016, 'difficulty': 100e12, 'adjustment': 1.08},
            ],
        }
        url_map = _all_endpoints_ok()
        url_map[HASHRATE_URL] = _json_resp(payload)

        with patch('requests.get', side_effect=_mock_get(url_map)), \
             patch('quant.collectors.btc_network.get_conn', return_value=_mock_db_close(60_000.0)), \
             patch.object(c, 'upsert_rows') as mock_upsert:
            c.collect()

        rows = mock_upsert.call_args.args[1]
        adj_row = next(r for r in rows if r['metric'] == 'BTC_DIFFICULTY_ADJ')
        assert adj_row['value'] == pytest.approx((1.08 - 1.0) * 100, rel=1e-4)


# ─────────────────────────────────────────────────────────────────────────────
# No-close skip: USD metrics omitted, network metrics still written
# ─────────────────────────────────────────────────────────────────────────────

class TestNoCloseBehavior:
    """When BTC close is unavailable, USD metrics are skipped; others are written."""

    def test_usd_metrics_omitted_when_no_close(self):
        """BTC_MINER_REVENUE_USD and BTC_TX_FEES_USD are not written without close."""
        c = BTCNetworkCollector(dry_run=True)

        with patch('requests.get', side_effect=_mock_get(_all_endpoints_ok())), \
             patch('quant.collectors.btc_network.get_conn', return_value=_mock_db_no_close()), \
             patch.object(c, 'upsert_rows') as mock_upsert:
            c.collect()

        rows = mock_upsert.call_args.args[1]
        metrics = {r['metric'] for r in rows}
        assert 'BTC_MINER_REVENUE_USD' not in metrics
        assert 'BTC_TX_FEES_USD' not in metrics

    def test_network_metrics_still_written_when_no_close(self):
        """BTC_HASHRATE, BTC_DIFFICULTY_ADJ, BTC_MEMPOOL_VBYTES, BTC_BLOCK_HEIGHT still write."""
        c = BTCNetworkCollector(dry_run=True)

        with patch('requests.get', side_effect=_mock_get(_all_endpoints_ok())), \
             patch('quant.collectors.btc_network.get_conn', return_value=_mock_db_no_close()), \
             patch.object(c, 'upsert_rows') as mock_upsert:
            c.collect()

        rows = mock_upsert.call_args.args[1]
        metrics = {r['metric'] for r in rows}
        assert 'BTC_HASHRATE' in metrics
        assert 'BTC_DIFFICULTY_ADJ' in metrics
        assert 'BTC_MEMPOOL_VBYTES' in metrics
        assert 'BTC_BLOCK_HEIGHT' in metrics

    def test_upsert_still_called_when_no_close(self):
        """upsert_rows IS called even without BTC close (network metrics go through)."""
        c = BTCNetworkCollector(dry_run=True)

        with patch('requests.get', side_effect=_mock_get(_all_endpoints_ok())), \
             patch('quant.collectors.btc_network.get_conn', return_value=_mock_db_no_close()), \
             patch.object(c, 'upsert_rows') as mock_upsert:
            c.collect()

        mock_upsert.assert_called_once()

    def test_db_exception_treated_as_no_close(self):
        """If DB raises, treat as no close — skip USD metrics, never crash."""
        c = BTCNetworkCollector(dry_run=True)

        with patch('requests.get', side_effect=_mock_get(_all_endpoints_ok())), \
             patch('quant.collectors.btc_network.get_conn', side_effect=RuntimeError("DB down")), \
             patch.object(c, 'upsert_rows') as mock_upsert:
            c.collect()  # must NOT raise

        rows = mock_upsert.call_args.args[1]
        metrics = {r['metric'] for r in rows}
        assert 'BTC_MINER_REVENUE_USD' not in metrics
        assert 'BTC_TX_FEES_USD' not in metrics


# ─────────────────────────────────────────────────────────────────────────────
# Per-endpoint partial-failure isolation
# ─────────────────────────────────────────────────────────────────────────────

class TestPartialFailureIsolation:
    """One bad endpoint never aborts the run; other metrics are still collected."""

    def test_reward_stats_fails_others_still_written(self):
        """reward-stats failure → no revenue/fee rows; hashrate/mempool/height still write."""
        c = BTCNetworkCollector(dry_run=True)
        url_map = _all_endpoints_ok()
        url_map[REWARD_STATS_URL] = ConnectionError("mempool.space timeout")

        with patch('requests.get', side_effect=_mock_get(url_map)), \
             patch('quant.collectors.btc_network.get_conn', return_value=_mock_db_close(60_000.0)), \
             patch.object(c, 'upsert_rows') as mock_upsert:
            c.collect()

        rows = mock_upsert.call_args.args[1]
        metrics = {r['metric'] for r in rows}
        assert 'BTC_MINER_REVENUE_USD' not in metrics
        assert 'BTC_TX_FEES_USD' not in metrics
        assert 'BTC_HASHRATE' in metrics
        assert 'BTC_MEMPOOL_VBYTES' in metrics
        assert 'BTC_BLOCK_HEIGHT' in metrics

    def test_hashrate_fails_others_still_written(self):
        """hashrate/1y failure → no hashrate/adj rows; revenue/mempool/height still write."""
        c = BTCNetworkCollector(dry_run=True)
        url_map = _all_endpoints_ok()
        url_map[HASHRATE_URL] = ConnectionError("timeout")

        with patch('requests.get', side_effect=_mock_get(url_map)), \
             patch('quant.collectors.btc_network.get_conn', return_value=_mock_db_close(60_000.0)), \
             patch.object(c, 'upsert_rows') as mock_upsert:
            c.collect()

        rows = mock_upsert.call_args.args[1]
        metrics = {r['metric'] for r in rows}
        assert 'BTC_HASHRATE' not in metrics
        assert 'BTC_DIFFICULTY_ADJ' not in metrics
        assert 'BTC_MINER_REVENUE_USD' in metrics
        assert 'BTC_MEMPOOL_VBYTES' in metrics

    def test_mempool_fails_others_still_written(self):
        """mempool endpoint failure → no BTC_MEMPOOL_VBYTES; others write."""
        c = BTCNetworkCollector(dry_run=True)
        url_map = _all_endpoints_ok()
        url_map[MEMPOOL_URL] = ConnectionError("timeout")

        with patch('requests.get', side_effect=_mock_get(url_map)), \
             patch('quant.collectors.btc_network.get_conn', return_value=_mock_db_close(60_000.0)), \
             patch.object(c, 'upsert_rows') as mock_upsert:
            c.collect()

        rows = mock_upsert.call_args.args[1]
        metrics = {r['metric'] for r in rows}
        assert 'BTC_MEMPOOL_VBYTES' not in metrics
        assert 'BTC_MINER_REVENUE_USD' in metrics
        assert 'BTC_HASHRATE' in metrics

    def test_height_fails_others_still_written(self):
        """height endpoint failure → no BTC_BLOCK_HEIGHT; others write."""
        c = BTCNetworkCollector(dry_run=True)
        url_map = _all_endpoints_ok()
        url_map[HEIGHT_URL] = ConnectionError("timeout")

        with patch('requests.get', side_effect=_mock_get(url_map)), \
             patch('quant.collectors.btc_network.get_conn', return_value=_mock_db_close(60_000.0)), \
             patch.object(c, 'upsert_rows') as mock_upsert:
            c.collect()

        rows = mock_upsert.call_args.args[1]
        metrics = {r['metric'] for r in rows}
        assert 'BTC_BLOCK_HEIGHT' not in metrics
        assert 'BTC_MINER_REVENUE_USD' in metrics

    def test_all_endpoints_fail_no_upsert(self):
        """If all endpoints fail, upsert_rows is NOT called (no degenerate rows)."""
        c = BTCNetworkCollector(dry_run=True)
        url_map = {
            REWARD_STATS_URL: ConnectionError("fail"),
            HASHRATE_URL:     ConnectionError("fail"),
            MEMPOOL_URL:      ConnectionError("fail"),
            HEIGHT_URL:       ConnectionError("fail"),
        }

        with patch('requests.get', side_effect=_mock_get(url_map)), \
             patch('quant.collectors.btc_network.get_conn', return_value=_mock_db_close(60_000.0)), \
             patch.object(c, 'upsert_rows') as mock_upsert:
            c.collect()

        mock_upsert.assert_not_called()

    def test_collect_does_not_raise_on_partial_failure(self):
        """Partial failure must never raise from collect() — logged as warning."""
        c = BTCNetworkCollector(dry_run=True)
        url_map = _all_endpoints_ok()
        url_map[REWARD_STATS_URL] = RuntimeError("500 Internal Server Error")

        with patch('requests.get', side_effect=_mock_get(url_map)), \
             patch('quant.collectors.btc_network.get_conn', return_value=_mock_db_close(60_000.0)), \
             patch.object(c, 'upsert_rows'):
            c.collect()  # must NOT raise


# ─────────────────────────────────────────────────────────────────────────────
# BTC_BLOCK_HEIGHT
# ─────────────────────────────────────────────────────────────────────────────

class TestBlockHeight:
    """BTC_BLOCK_HEIGHT is fetched from a bare-integer text response."""

    def test_collector_writes_block_height(self):
        """Collector writes BTC_BLOCK_HEIGHT from the tip height endpoint."""
        c = BTCNetworkCollector(dry_run=True)
        url_map = _all_endpoints_ok()
        url_map[HEIGHT_URL] = _text_resp('956086')

        with patch('requests.get', side_effect=_mock_get(url_map)), \
             patch('quant.collectors.btc_network.get_conn', return_value=_mock_db_close(60_000.0)), \
             patch.object(c, 'upsert_rows') as mock_upsert:
            c.collect()

        rows = mock_upsert.call_args.args[1]
        height_row = next(r for r in rows if r['metric'] == 'BTC_BLOCK_HEIGHT')
        assert height_row['value'] == 956086
        assert height_row['source'] == 'mempool.space'


# ─────────────────────────────────────────────────────────────────────────────
# BTC_MEMPOOL_VBYTES
# ─────────────────────────────────────────────────────────────────────────────

class TestMempoolVbytes:
    """BTC_MEMPOOL_VBYTES = mempool.vsize (raw integer virtual bytes)."""

    def test_collector_writes_mempool_vbytes(self):
        vsize = 35_000_000
        c = BTCNetworkCollector(dry_run=True)
        url_map = _all_endpoints_ok()
        url_map[MEMPOOL_URL] = _json_resp(_mempool_payload(vsize=vsize))

        with patch('requests.get', side_effect=_mock_get(url_map)), \
             patch('quant.collectors.btc_network.get_conn', return_value=_mock_db_close(60_000.0)), \
             patch.object(c, 'upsert_rows') as mock_upsert:
            c.collect()

        rows = mock_upsert.call_args.args[1]
        vbytes_row = next(r for r in rows if r['metric'] == 'BTC_MEMPOOL_VBYTES')
        assert vbytes_row['value'] == vsize
        assert vbytes_row['source'] == 'mempool.space'


# ─────────────────────────────────────────────────────────────────────────────
# Row shape contract
# ─────────────────────────────────────────────────────────────────────────────

class TestRowShape:
    """Every row written to quant.onchain has the correct column set."""

    def test_all_rows_have_required_keys(self):
        """Each row must have: ts, asset, metric, value, source."""
        c = BTCNetworkCollector(dry_run=True)

        with patch('requests.get', side_effect=_mock_get(_all_endpoints_ok())), \
             patch('quant.collectors.btc_network.get_conn', return_value=_mock_db_close(60_000.0)), \
             patch.object(c, 'upsert_rows') as mock_upsert:
            c.collect()

        rows = mock_upsert.call_args.args[1]
        assert len(rows) > 0
        for row in rows:
            for key in ('ts', 'asset', 'metric', 'value', 'source'):
                assert key in row, f"Missing key '{key}' in row: {row}"

    def test_all_rows_asset_is_btc(self):
        """Every written row has asset='BTC'."""
        c = BTCNetworkCollector(dry_run=True)

        with patch('requests.get', side_effect=_mock_get(_all_endpoints_ok())), \
             patch('quant.collectors.btc_network.get_conn', return_value=_mock_db_close(60_000.0)), \
             patch.object(c, 'upsert_rows') as mock_upsert:
            c.collect()

        rows = mock_upsert.call_args.args[1]
        for row in rows:
            assert row['asset'] == 'BTC'

    def test_upsert_called_with_correct_table_and_conflict_cols(self):
        """upsert_rows receives 'quant.onchain' and conflict_cols=['ts','asset','metric']."""
        c = BTCNetworkCollector(dry_run=True)

        with patch('requests.get', side_effect=_mock_get(_all_endpoints_ok())), \
             patch('quant.collectors.btc_network.get_conn', return_value=_mock_db_close(60_000.0)), \
             patch.object(c, 'upsert_rows') as mock_upsert:
            c.collect()

        call_args = mock_upsert.call_args
        table = call_args.args[0]
        conflict_cols = call_args.kwargs.get('conflict_cols') or call_args.args[2]
        assert table == 'quant.onchain'
        assert set(conflict_cols) == {'ts', 'asset', 'metric'}

    def test_full_run_writes_six_metrics(self):
        """When all endpoints succeed and BTC close is available, all 6 metrics are written."""
        c = BTCNetworkCollector(dry_run=True)

        with patch('requests.get', side_effect=_mock_get(_all_endpoints_ok())), \
             patch('quant.collectors.btc_network.get_conn', return_value=_mock_db_close(60_000.0)), \
             patch.object(c, 'upsert_rows') as mock_upsert:
            c.collect()

        rows = mock_upsert.call_args.args[1]
        metrics = {r['metric'] for r in rows}
        expected = {
            'BTC_MINER_REVENUE_USD', 'BTC_TX_FEES_USD',
            'BTC_HASHRATE', 'BTC_DIFFICULTY_ADJ',
            'BTC_MEMPOOL_VBYTES', 'BTC_BLOCK_HEIGHT',
        }
        assert metrics == expected

    def test_user_agent_sent_on_all_requests(self):
        """All HTTP GET calls carry the descriptive User-Agent header."""
        c = BTCNetworkCollector(dry_run=True)
        captured_headers = []
        endpoint_map = _all_endpoints_ok()

        def _capturing_get(url, **kwargs):
            captured_headers.append(kwargs.get('headers', {}))
            return endpoint_map[url]

        with patch('requests.get', side_effect=_capturing_get), \
             patch('quant.collectors.btc_network.get_conn', return_value=_mock_db_close(60_000.0)), \
             patch.object(c, 'upsert_rows'):
            c.collect()

        assert len(captured_headers) == 4  # 4 endpoints
        for headers in captured_headers:
            assert headers.get('User-Agent') == USER_AGENT
