"""Tests for build_btc_network_block() and the _btc_supply_from_height() helper.

Covers:
- _btc_supply_from_height() pure-function: epoch boundaries, partial epoch,
  circulating supply at a known height, blocks-to-next-halving.
- build_btc_network_block() with a mocked fetch callable:
    - full data render (all 6 metrics present)
    - partial data (only some metrics present) — block still renders what is there
    - zero data (empty fetch) — returns empty string
    - fetch exception — returns empty string gracefully
- Non-advice language: must not contain buy/sell/recommend.
- USD humanisation (_humanize_usd): M/B/K suffixes.
"""

from __future__ import annotations

from datetime import date

import pytest


# ─── helpers ─────────────────────────────────────────────────────────────────

def _make_row(metric: str, value: float, ts: str = "2026-06-30") -> dict:
    return {"metric": metric, "value": value, "ts": date.fromisoformat(ts)}


def _full_rows(ts: str = "2026-06-30") -> list[dict]:
    return [
        _make_row("BTC_HASHRATE",          620.5,        ts),
        _make_row("BTC_MINER_REVENUE_USD", 48_300_000.0, ts),
        _make_row("BTC_TX_FEES_USD",        1_200_000.0, ts),
        _make_row("BTC_MEMPOOL_VBYTES",    85_000_000.0, ts),
        _make_row("BTC_DIFFICULTY_ADJ",         7.96,    ts),
        _make_row("BTC_BLOCK_HEIGHT",     956_086.0,     ts),
    ]


# ─── _btc_supply_from_height ──────────────────────────────────────────────────

class TestBtcSupplyFromHeight:
    @pytest.fixture(autouse=True)
    def _import(self):
        from morning_engine.report_blocks import _btc_supply_from_height
        self.f = _btc_supply_from_height

    # --- epoch boundary correctness ---

    def test_height_0_epoch0(self):
        supply, subsidy, _ = self.f(0)
        assert subsidy == pytest.approx(50.0)
        assert supply == pytest.approx(50.0)   # block 0 only

    def test_height_209999_end_of_epoch0(self):
        supply, subsidy, _ = self.f(209_999)
        assert subsidy == pytest.approx(50.0)
        assert supply == pytest.approx(50.0 * 210_000)  # 10,500,000 BTC

    def test_height_210000_start_of_epoch1(self):
        supply, subsidy, _ = self.f(210_000)
        assert subsidy == pytest.approx(25.0)
        # all of epoch 0 + 1 block of epoch 1
        assert supply == pytest.approx(10_500_000.0 + 25.0)

    def test_height_840000_start_of_epoch4(self):
        """April 2024 halving block — first block at 3.125 BTC."""
        supply, subsidy, blocks_to_halving = self.f(840_000)
        assert subsidy == pytest.approx(3.125)
        # epochs 0-3 sum + 1 block of epoch 4
        expected_supply = (
            50.0 * 210_000      # epoch 0
            + 25.0 * 210_000    # epoch 1
            + 12.5 * 210_000    # epoch 2
            + 6.25 * 210_000    # epoch 3
            + 3.125             # block 840,000 itself
        )
        assert supply == pytest.approx(expected_supply)
        assert blocks_to_halving == 210_000  # exactly one full epoch left

    def test_height_956086_current_epoch4(self):
        """
        At height 956,086 (epoch 4, post-2024 halving):
          - subsidy = 3.125 BTC/block
          - Epochs 0-3 complete:
              e0: 50   × 210,000 = 10,500,000
              e1: 25   × 210,000 =  5,250,000
              e2: 12.5 × 210,000 =  2,625,000
              e3:  6.25× 210,000 =  1,312,500  ← 6.25×210k=1,312,500 (not 1,562,500)
                                    ──────────
                              sum = 19,687,500
          - Epoch 4 partial: (956,086 − 840,000 + 1) × 3.125
                           = 116,087 × 3.125 = 362,771.875
          - Total: 19,687,500 + 362,771.875 = 20,050,271.875 BTC ≈ 20.05M
          - blocks_to_halving = 1,050,000 − 956,086 = 93,914
        """
        supply, subsidy, blocks_to_halving = self.f(956_086)
        assert subsidy == pytest.approx(3.125)
        assert supply == pytest.approx(20_050_271.875, rel=1e-6)
        assert blocks_to_halving == 1_050_000 - 956_086  # = 93,914

    def test_blocks_to_halving_formula(self):
        """blocks_to_halving always equals (epoch+1)*210_000 - height."""
        for height in (500_000, 750_000, 840_001, 956_086, 1_049_999):
            _, _, btnh = self.f(height)
            epoch = height // 210_000
            expected = (epoch + 1) * 210_000 - height
            assert btnh == expected, f"height={height}"

    def test_subsidy_halves_each_epoch(self):
        subsidies = [self.f(e * 210_000)[1] for e in range(6)]
        for i in range(1, len(subsidies)):
            assert subsidies[i] == pytest.approx(subsidies[i - 1] / 2)


# ─── _humanize_usd ────────────────────────────────────────────────────────────

class TestHumanizeUsd:
    @pytest.fixture(autouse=True)
    def _import(self):
        from morning_engine.report_blocks import _humanize_usd
        self.f = _humanize_usd

    def test_millions(self):
        assert self.f(48_300_000.0) == "$48.3M"

    def test_billions(self):
        assert self.f(2_500_000_000.0) == "$2.50B"

    def test_thousands(self):
        assert self.f(5_400.0) == "$5.4K"

    def test_sub_thousand(self):
        assert self.f(500.0) == "$500"

    def test_exact_million(self):
        assert self.f(1_000_000.0) == "$1.0M"


# ─── build_btc_network_block ──────────────────────────────────────────────────

class TestBuildBtcNetworkBlock:
    def _run(self, rows: list[dict]) -> str:
        from morning_engine.report_blocks import build_btc_network_block
        return build_btc_network_block(fetch=lambda: rows)

    # --- empty / error handling ---

    def test_returns_empty_string_when_no_rows(self):
        assert self._run([]) == ""

    def test_returns_empty_string_when_fetch_raises(self):
        from morning_engine.report_blocks import build_btc_network_block

        def _fail():
            raise RuntimeError("DB gone")

        assert build_btc_network_block(fetch=_fail) == ""

    # --- full-data render ---

    def test_heading_contains_bitcoin_network(self):
        result = self._run(_full_rows())
        assert "## Bitcoin Network Fundamentals" in result

    def test_heading_contains_date(self):
        result = self._run(_full_rows("2026-06-30"))
        assert "2026-06-30" in result

    def test_hashrate_line_present(self):
        result = self._run(_full_rows())
        assert "Hashrate:" in result
        assert "EH/s" in result

    def test_hashrate_value(self):
        result = self._run(_full_rows())
        assert "620.5 EH/s" in result

    def test_miner_revenue_humanized(self):
        result = self._run(_full_rows())
        assert "$48.3M/day" in result

    def test_tx_fees_humanized(self):
        result = self._run(_full_rows())
        # 1,200,000 → $1.2M
        assert "$1.2M/day" in result

    def test_mempool_in_mb(self):
        result = self._run(_full_rows())
        # 85,000,000 vbytes / 1e6 = 85.0 MB
        assert "85.0 MB" in result

    def test_difficulty_adj_with_sign(self):
        result = self._run(_full_rows())
        assert "+7.96%" in result

    def test_negative_difficulty_adj(self):
        rows = [_make_row("BTC_DIFFICULTY_ADJ", -3.14)]
        result = self._run(rows)
        assert "-3.14%" in result

    def test_block_height_line_present(self):
        result = self._run(_full_rows())
        assert "Block Height:" in result
        assert "956,086" in result

    def test_supply_and_issuance_in_block_height_line(self):
        result = self._run(_full_rows())
        # supply ~20.05M BTC at height 956,086; issuance 3.125 BTC/block
        assert "Supply:" in result
        assert "20.05M BTC" in result
        assert "Issuance: 3.1250 BTC/block" in result

    def test_blocks_to_halving_present(self):
        result = self._run(_full_rows())
        # 1,050,000 - 956,086 = 93,914
        assert "93,914" in result

    def test_result_is_multiline(self):
        result = self._run(_full_rows())
        assert len(result.split("\n")) >= 4

    # --- partial data ---

    def test_partial_only_hashrate(self):
        rows = [_make_row("BTC_HASHRATE", 600.0)]
        result = self._run(rows)
        assert result != ""
        assert "Hashrate:" in result
        # Other metrics must not appear
        assert "Miner Revenue:" not in result
        assert "TX Fees:" not in result

    def test_partial_only_miner_revenue_and_fees(self):
        rows = [
            _make_row("BTC_MINER_REVENUE_USD", 50_000_000.0),
            _make_row("BTC_TX_FEES_USD", 2_000_000.0),
        ]
        result = self._run(rows)
        assert "Miner Revenue:" in result
        assert "TX Fees:" in result
        assert "Hashrate:" not in result
        assert "Block Height:" not in result

    def test_partial_only_block_height(self):
        rows = [_make_row("BTC_BLOCK_HEIGHT", 956_086.0)]
        result = self._run(rows)
        assert result != ""
        assert "Block Height:" in result
        assert "Hashrate:" not in result

    def test_partial_only_difficulty(self):
        rows = [_make_row("BTC_DIFFICULTY_ADJ", 7.96)]
        result = self._run(rows)
        assert "Difficulty Adj: +7.96%" in result

    # --- non-advice language ---

    def test_no_buy_language(self):
        result = self._run(_full_rows())
        assert "buy" not in result.lower()

    def test_no_sell_language(self):
        result = self._run(_full_rows())
        assert "sell" not in result.lower()

    def test_no_recommend_language(self):
        result = self._run(_full_rows())
        assert "recommend" not in result.lower()

    def test_no_should_language(self):
        result = self._run(_full_rows())
        assert "should" not in result.lower()

    def test_non_advice_disclaimer_present(self):
        result = self._run(_full_rows())
        assert "not advice" in result.lower()

    # --- edge: zero difficulty adjustment ---

    def test_zero_difficulty_adj(self):
        rows = [_make_row("BTC_DIFFICULTY_ADJ", 0.0)]
        result = self._run(rows)
        # Zero difficulty should render with "+" sign (>=0 branch)
        assert "+0.00%" in result

    # --- edge: very large mempool ---

    def test_large_mempool(self):
        rows = [_make_row("BTC_MEMPOOL_VBYTES", 500_000_000.0)]
        result = self._run(rows)
        assert "500.0 MB" in result
