"""
Tests for quant.collectors.stablecoin_onchain — pure math, no real network/DB.

TDD: written BEFORE the implementation, following tests/test_mayer.py style.
All network (requests.post / requests.get) and DB (carrac.db_pool.get_quant_conn) calls
are monkeypatched — no external dependencies.
"""

import pytest
from unittest.mock import patch, MagicMock

from quant.collectors.stablecoin_onchain import (
    StablecoinOnchainCollector,
    DEC,
    ETH_RPCS,
    USDT_ETH,
    USDC_ETH,
    SEL_TOTAL_SUPPLY,
    USDT_ETH_TREASURY_DATA,
)


# ─────────────────────────────────────────────────────────────────────────────
# _eth_call — hex decode from JSON-RPC
# ─────────────────────────────────────────────────────────────────────────────

class TestEthCallHexDecode:
    """_eth_call returns int(result, 16) from a JSON-RPC response."""

    def _mock_resp(self, raw_int: int) -> MagicMock:
        """Build a mock requests.post that returns one successful hex result."""
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {'result': '0x' + hex(raw_int)[2:].zfill(64)}
        return resp

    def test_large_integer_decoded_correctly(self):
        """1e15 raw units → int decoded losslessly."""
        raw = 1_000_000_000 * DEC  # 1e15
        with patch('requests.post', return_value=self._mock_resp(raw)):
            c = StablecoinOnchainCollector(dry_run=True)
            assert c._eth_call(USDT_ETH, SEL_TOTAL_SUPPLY) == raw

    def test_result_0x_empty_falls_through_all_rpcs_raises(self):
        """'0x' (empty/zero result) must exhaust all RPCs then raise RuntimeError."""
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {'result': '0x'}
        with patch('requests.post', return_value=resp):
            c = StablecoinOnchainCollector(dry_run=True)
            with pytest.raises(RuntimeError, match='all ETH RPCs failed'):
                c._eth_call(USDT_ETH, SEL_TOTAL_SUPPLY)

    def test_all_rpcs_fail_with_exception_raises(self):
        """All RPCs raising an exception → RuntimeError."""
        with patch('requests.post', side_effect=Exception('network error')):
            c = StablecoinOnchainCollector(dry_run=True)
            with pytest.raises(RuntimeError, match='all ETH RPCs failed'):
                c._eth_call(USDT_ETH, SEL_TOTAL_SUPPLY)

    def test_exhausts_all_three_rpcs_before_raising(self):
        """Tries every RPC in ETH_RPCS (3 total) before giving up."""
        with patch('requests.post', side_effect=Exception('fail')) as mock_post:
            c = StablecoinOnchainCollector(dry_run=True)
            with pytest.raises(RuntimeError):
                c._eth_call(USDT_ETH, SEL_TOTAL_SUPPLY)
            assert mock_post.call_count == len(ETH_RPCS)

    def test_falls_back_to_second_rpc_on_first_failure(self):
        """If first RPC fails, second is tried and succeeds."""
        raw = 80_000_000 * DEC

        def side_effect(*args, **kwargs):
            if args[0] == ETH_RPCS[0]:
                raise Exception('primary down')
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            resp.json.return_value = {'result': '0x' + hex(raw)[2:].zfill(64)}
            return resp

        with patch('requests.post', side_effect=side_effect):
            c = StablecoinOnchainCollector(dry_run=True)
            assert c._eth_call(USDT_ETH, SEL_TOTAL_SUPPLY) == raw


# ─────────────────────────────────────────────────────────────────────────────
# Treasury subtraction + /1e6 scaling
# ─────────────────────────────────────────────────────────────────────────────

class TestTreasurySubtraction:
    """eth_circ = (totalSupply − balanceOf(treasury)) / DEC"""

    def test_subtraction_and_scaling(self):
        """ETH USDT circulating = (totalSupply − treasury) / 1e6."""
        total_supply_raw = 100_000_000 * DEC   # 100 000 000 USD worth of raw units
        treasury_raw     =  10_000_000 * DEC   # 10 000 000 USD
        expected_circ    =  90_000_000.0        # 90 000 000 USD

        c = StablecoinOnchainCollector(dry_run=True)

        def fake_eth_call(to, data):
            if to == USDT_ETH and data == SEL_TOTAL_SUPPLY:
                return total_supply_raw
            if to == USDT_ETH and data == USDT_ETH_TREASURY_DATA:
                return treasury_raw
            return 0  # USDC call — we don't test USDC here

        with patch.object(c, '_eth_call', side_effect=fake_eth_call), \
             patch.object(c, '_tron_total_supply', return_value=0), \
             patch.object(c, 'upsert_rows') as mock_upsert, \
             patch.object(c, '_compute_stablecoin_dominance'):
            c.collect()
            rows = mock_upsert.call_args_list[0].args[1]
            usdt_row = next(r for r in rows if r['metric'] == 'STABLECOIN_USDT_SUPPLY')
            assert usdt_row['value'] == pytest.approx(expected_circ)

    def test_zero_treasury_gives_full_supply(self):
        """If treasury balance = 0, circulating = totalSupply / DEC."""
        total_supply_raw = 50_000_000 * DEC

        c = StablecoinOnchainCollector(dry_run=True)

        def fake_eth_call(to, data):
            if data == SEL_TOTAL_SUPPLY:
                return total_supply_raw
            return 0  # treasury (and USDC)

        with patch.object(c, '_eth_call', side_effect=fake_eth_call), \
             patch.object(c, '_tron_total_supply', return_value=0), \
             patch.object(c, 'upsert_rows') as mock_upsert, \
             patch.object(c, '_compute_stablecoin_dominance'):
            c.collect()
            rows = mock_upsert.call_args_list[0].args[1]
            usdt_row = next(r for r in rows if r['metric'] == 'STABLECOIN_USDT_SUPPLY')
            assert usdt_row['value'] == pytest.approx(50_000_000.0)

    def test_scaling_divides_by_1e6(self):
        """Verify DEC=1_000_000 is applied — raw 1_000_000 → 1.0 unit."""
        c = StablecoinOnchainCollector(dry_run=True)

        def fake_eth_call(to, data):
            if data == SEL_TOTAL_SUPPLY:
                return DEC  # exactly 1 "dollar" worth
            return 0

        with patch.object(c, '_eth_call', side_effect=fake_eth_call), \
             patch.object(c, '_tron_total_supply', return_value=0), \
             patch.object(c, 'upsert_rows') as mock_upsert, \
             patch.object(c, '_compute_stablecoin_dominance'):
            c.collect()
            rows = mock_upsert.call_args_list[0].args[1]
            usdt_row = next(r for r in rows if r['metric'] == 'STABLECOIN_USDT_SUPPLY')
            assert usdt_row['value'] == pytest.approx(1.0)


# ─────────────────────────────────────────────────────────────────────────────
# USDT total = ETH circulating + Tron total
# ─────────────────────────────────────────────────────────────────────────────

class TestUSDTTotal:
    """USDT supply = ETH_circ + Tron_circ."""

    def test_eth_plus_tron_sum(self):
        """USDT = (ETH_totalSupply − treasury)/1e6 + Tron_totalSupply/1e6."""
        eth_supply_raw   = 70_000_000 * DEC   # 70 000 000 USD
        eth_treasury_raw =          0
        tron_raw         = 50_000_000 * DEC   # 50 000 000 USD
        expected         = 120_000_000.0

        c = StablecoinOnchainCollector(dry_run=True)

        def fake_eth_call(to, data):
            if data == SEL_TOTAL_SUPPLY:
                return eth_supply_raw
            return eth_treasury_raw  # treasury = 0

        with patch.object(c, '_eth_call', side_effect=fake_eth_call), \
             patch.object(c, '_tron_total_supply', return_value=tron_raw), \
             patch.object(c, 'upsert_rows') as mock_upsert, \
             patch.object(c, '_compute_stablecoin_dominance'):
            c.collect()
            rows = mock_upsert.call_args_list[0].args[1]
            usdt_row = next(r for r in rows if r['metric'] == 'STABLECOIN_USDT_SUPPLY')
            assert usdt_row['value'] == pytest.approx(expected)

    def test_tron_zero_uses_eth_circulating_only(self):
        """If Tron = 0, USDT total = ETH circulating."""
        eth_supply_raw = 80_000_000 * DEC
        c = StablecoinOnchainCollector(dry_run=True)

        def fake_eth_call(to, data):
            if data == SEL_TOTAL_SUPPLY:
                return eth_supply_raw
            return 0

        with patch.object(c, '_eth_call', side_effect=fake_eth_call), \
             patch.object(c, '_tron_total_supply', return_value=0), \
             patch.object(c, 'upsert_rows') as mock_upsert, \
             patch.object(c, '_compute_stablecoin_dominance'):
            c.collect()
            rows = mock_upsert.call_args_list[0].args[1]
            usdt_row = next(r for r in rows if r['metric'] == 'STABLECOIN_USDT_SUPPLY')
            assert usdt_row['value'] == pytest.approx(80_000_000.0)


# ─────────────────────────────────────────────────────────────────────────────
# USDC scaling
# ─────────────────────────────────────────────────────────────────────────────

class TestUSDCScaling:
    """USDC = ETH totalSupply / 1e6 only (no treasury subtraction)."""

    def test_usdc_raw_divided_by_1e6(self):
        usdc_raw = 45_000_000 * DEC  # 45 000 000 USD
        c = StablecoinOnchainCollector(dry_run=True)

        def fake_eth_call(to, data):
            if to == USDC_ETH:
                return usdc_raw
            return 0  # USDT calls → 0

        with patch.object(c, '_eth_call', side_effect=fake_eth_call), \
             patch.object(c, '_tron_total_supply', return_value=0), \
             patch.object(c, 'upsert_rows') as mock_upsert, \
             patch.object(c, '_compute_stablecoin_dominance'):
            c.collect()
            rows = mock_upsert.call_args_list[0].args[1]
            usdc_row = next(r for r in rows if r['metric'] == 'STABLECOIN_USDC_SUPPLY')
            assert usdc_row['value'] == pytest.approx(45_000_000.0)


# ─────────────────────────────────────────────────────────────────────────────
# Partial-failure guard — never write 0/None, isolate USDT vs USDC
# ─────────────────────────────────────────────────────────────────────────────

class TestPartialFailureGuard:
    """If USDT fails, USDC still writes. If both fail, nothing is written."""

    def test_usdt_fails_usdc_still_written(self):
        """USDT exception → USDC row still reaches upsert_rows; USDT does NOT."""
        usdc_raw = 40_000_000 * DEC
        c = StablecoinOnchainCollector(dry_run=True)

        def fake_eth_call(to, data):
            if to == USDT_ETH:
                raise RuntimeError('USDT ETH RPC failed')
            return usdc_raw  # USDC succeeds

        with patch.object(c, '_eth_call', side_effect=fake_eth_call), \
             patch.object(c, '_tron_total_supply', side_effect=RuntimeError('Tron failed')), \
             patch.object(c, 'upsert_rows') as mock_upsert, \
             patch.object(c, '_compute_stablecoin_dominance'):
            c.collect()
            assert mock_upsert.called, 'upsert_rows must be called for USDC'
            rows = mock_upsert.call_args_list[0].args[1]
            metrics = {r['metric'] for r in rows}
            assert 'STABLECOIN_USDC_SUPPLY' in metrics
            assert 'STABLECOIN_USDT_SUPPLY' not in metrics

    def test_both_fail_upsert_not_called(self):
        """Both USDT and USDC fail → upsert_rows is NOT called (no 0 row written)."""
        c = StablecoinOnchainCollector(dry_run=True)

        with patch.object(c, '_eth_call', side_effect=RuntimeError('all fail')), \
             patch.object(c, '_tron_total_supply', side_effect=RuntimeError('tron fail')), \
             patch.object(c, 'upsert_rows') as mock_upsert, \
             patch.object(c, '_compute_stablecoin_dominance'):
            c.collect()
            mock_upsert.assert_not_called()

    def test_usdc_fails_usdt_still_written(self):
        """USDC exception → USDT row is still written; USDC does NOT appear."""
        total_supply_raw = 100_000_000 * DEC
        c = StablecoinOnchainCollector(dry_run=True)

        def fake_eth_call(to, data):
            if to == USDC_ETH:
                raise RuntimeError('USDC RPC failed')
            # USDT: distinguish totalSupply vs balanceOf(treasury)
            if data == SEL_TOTAL_SUPPLY:
                return total_supply_raw
            return 0  # treasury balance = 0

        with patch.object(c, '_eth_call', side_effect=fake_eth_call), \
             patch.object(c, '_tron_total_supply', return_value=0), \
             patch.object(c, 'upsert_rows') as mock_upsert, \
             patch.object(c, '_compute_stablecoin_dominance'):
            c.collect()
            assert mock_upsert.called
            rows = mock_upsert.call_args_list[0].args[1]
            metrics = {r['metric'] for r in rows}
            assert 'STABLECOIN_USDT_SUPPLY' in metrics
            assert 'STABLECOIN_USDC_SUPPLY' not in metrics

    def test_zero_value_is_not_prevented_if_rpc_succeeded(self):
        """Guard is about exceptions, not zero values from successful calls.
        If the chain genuinely returns 0, we write 0 (edge case — tests the guard
        is exception-based, not value-based)."""
        c = StablecoinOnchainCollector(dry_run=True)

        # Both RPCs succeed returning 0 — collect() should still call upsert_rows
        def fake_eth_call(to, data):
            return 0

        with patch.object(c, '_eth_call', side_effect=fake_eth_call), \
             patch.object(c, '_tron_total_supply', return_value=0), \
             patch.object(c, 'upsert_rows') as mock_upsert, \
             patch.object(c, '_compute_stablecoin_dominance'):
            c.collect()
            # rows list is non-empty (two 0-value rows), so upsert IS called
            assert mock_upsert.called


# ─────────────────────────────────────────────────────────────────────────────
# Dominance math
# ─────────────────────────────────────────────────────────────────────────────

class TestDominanceMath:
    """STABLECOIN_DOMINANCE = (USDT + USDC supply) / total_mcap * 100, rounded 4dp."""

    def _make_db_mock(self, supply_rows):
        """Build a mock DB connection returning supply_rows from cursor.fetchall()."""
        mock_cur = MagicMock()
        mock_cur.fetchall.return_value = supply_rows
        mock_conn = MagicMock()
        mock_conn.__enter__.return_value = mock_conn
        mock_conn.__exit__.return_value = False
        mock_conn.cursor.return_value = mock_cur
        return mock_conn

    def _make_mcap_resp(self, mcap: float) -> MagicMock:
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {'market_cap_usd': mcap}
        return resp

    def test_dominance_formula_hand_computed(self):
        """(100B + 40B) / 3.5T * 100 = 4.0 exactly."""
        usdt = 100_000_000_000.0
        usdc =  40_000_000_000.0
        mcap  = 3_500_000_000_000.0
        expected = round((usdt + usdc) / mcap * 100, 4)  # 4.0

        mock_conn = self._make_db_mock([
            {'metric': 'STABLECOIN_USDT_SUPPLY', 'value': usdt},
            {'metric': 'STABLECOIN_USDC_SUPPLY', 'value': usdc},
        ])

        c = StablecoinOnchainCollector(dry_run=True)
        with patch('carrac.db_pool.get_quant_conn', return_value=mock_conn), \
             patch('requests.get', return_value=self._make_mcap_resp(mcap)), \
             patch.object(c, 'upsert_rows') as mock_upsert:
            c._compute_stablecoin_dominance('2026-06-30')

        assert mock_upsert.called
        row = mock_upsert.call_args_list[0].args[1][0]
        assert row['metric'] == 'STABLECOIN_DOMINANCE'
        assert row['value'] == pytest.approx(expected)
        assert row['asset'] == '_GLOBAL_'

    def test_dominance_rounded_to_4_decimal_places(self):
        """Result precision is exactly 4 decimal places."""
        usdt = 140_123_456_789.0
        usdc =  35_987_654_321.0
        mcap = 3_456_789_012_345.0

        mock_conn = self._make_db_mock([
            {'metric': 'STABLECOIN_USDT_SUPPLY', 'value': usdt},
            {'metric': 'STABLECOIN_USDC_SUPPLY', 'value': usdc},
        ])

        c = StablecoinOnchainCollector(dry_run=True)
        with patch('carrac.db_pool.get_quant_conn', return_value=mock_conn), \
             patch('requests.get', return_value=self._make_mcap_resp(mcap)), \
             patch.object(c, 'upsert_rows') as mock_upsert:
            c._compute_stablecoin_dominance('2026-06-30')

        row = mock_upsert.call_args_list[0].args[1][0]
        expected = round((usdt + usdc) / mcap * 100, 4)
        assert row['value'] == expected
        # Confirm it IS genuinely 4dp (not just close enough)
        assert row['value'] == round(row['value'], 4)

    def test_zero_supply_skips_dominance(self):
        """If both supplies are absent, dominance computation exits early."""
        mock_conn = self._make_db_mock([])  # empty — no supply rows

        c = StablecoinOnchainCollector(dry_run=True)
        with patch('carrac.db_pool.get_quant_conn', return_value=mock_conn), \
             patch('requests.get') as mock_get, \
             patch.object(c, 'upsert_rows') as mock_upsert:
            c._compute_stablecoin_dominance('2026-06-30')

        mock_upsert.assert_not_called()
        mock_get.assert_not_called()  # CoinPaprika not hit if no supply

    def test_zero_mcap_skips_dominance(self):
        """If CoinPaprika returns 0 market_cap_usd, do not write dominance."""
        mock_conn = self._make_db_mock([
            {'metric': 'STABLECOIN_USDT_SUPPLY', 'value': 100_000_000_000.0},
        ])

        c = StablecoinOnchainCollector(dry_run=True)
        with patch('carrac.db_pool.get_quant_conn', return_value=mock_conn), \
             patch('requests.get', return_value=self._make_mcap_resp(0)), \
             patch.object(c, 'upsert_rows') as mock_upsert:
            c._compute_stablecoin_dominance('2026-06-30')

        mock_upsert.assert_not_called()

    def test_dominance_source_field(self):
        """source must be 'onchain+coinpaprika'."""
        usdt = 100_000_000_000.0
        usdc =  40_000_000_000.0
        mcap = 3_500_000_000_000.0

        mock_conn = self._make_db_mock([
            {'metric': 'STABLECOIN_USDT_SUPPLY', 'value': usdt},
            {'metric': 'STABLECOIN_USDC_SUPPLY', 'value': usdc},
        ])

        c = StablecoinOnchainCollector(dry_run=True)
        with patch('carrac.db_pool.get_quant_conn', return_value=mock_conn), \
             patch('requests.get', return_value=self._make_mcap_resp(mcap)), \
             patch.object(c, 'upsert_rows') as mock_upsert:
            c._compute_stablecoin_dominance('2026-06-30')

        row = mock_upsert.call_args_list[0].args[1][0]
        assert row['source'] == 'onchain+coinpaprika'


# ─────────────────────────────────────────────────────────────────────────────
# Row shape contract (_row static method)
# ─────────────────────────────────────────────────────────────────────────────

class TestRowShape:
    """_row() must produce the exact column set the quant.sentiment table expects."""

    def test_row_has_all_required_keys(self):
        row = StablecoinOnchainCollector._row('2026-06-30', 'STABLECOIN_USDT_SUPPLY', 100_000.0)
        for key in ('ts', 'metric', 'asset', 'value', 'source'):
            assert key in row

    def test_row_asset_is_global(self):
        row = StablecoinOnchainCollector._row('2026-06-30', 'STABLECOIN_USDT_SUPPLY', 1.0)
        assert row['asset'] == '_GLOBAL_'

    def test_row_source_is_onchain(self):
        row = StablecoinOnchainCollector._row('2026-06-30', 'STABLECOIN_USDT_SUPPLY', 1.0)
        assert row['source'] == 'onchain'

    def test_row_value_rounded_to_2dp(self):
        row = StablecoinOnchainCollector._row('2026-06-30', 'STABLECOIN_USDT_SUPPLY', 123_456.789123)
        assert row['value'] == round(123_456.789123, 2)

    def test_row_metric_preserved(self):
        row = StablecoinOnchainCollector._row('2026-06-30', 'STABLECOIN_USDC_SUPPLY', 1.0)
        assert row['metric'] == 'STABLECOIN_USDC_SUPPLY'

    def test_row_ts_preserved(self):
        row = StablecoinOnchainCollector._row('2026-06-30', 'STABLECOIN_USDT_SUPPLY', 1.0)
        assert row['ts'] == '2026-06-30'
