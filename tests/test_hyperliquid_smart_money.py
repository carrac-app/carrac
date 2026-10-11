"""
Tests for quant.collectors.hyperliquid_smart_money — pure math, no real network/DB.

TDD: written BEFORE the implementation, following tests/test_stablecoin_onchain.py style.
All network (requests.get / requests.post) and DB calls are monkeypatched — no
external dependencies.

Coverage contract (per build spec):
  - szi sign → direction (positive=long, negative=short)
  - 25% per-wallet whale cap
  - <3-wallet guard (skip asset)
  - <$1M gross guard (skip asset)
  - bias_raw→0-100 mapping: all-long=100, all-short=0, balanced=50
  - liquidationPx=null safety (no crash on null field)
  - per-wallet try/except: one 500 wallet does not abort the run
"""

import pytest
from collections import defaultdict
from unittest.mock import patch, MagicMock

from quant.collectors.hyperliquid_smart_money import (
    HyperliquidSmartMoneyCollector,
    TOP_N,
)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _leaderboard_resp(wallets_pnl):
    """Build a fake leaderboard JSON response.

    wallets_pnl: list of (eth_address, alltime_pnl) tuples.
    """
    rows = []
    for addr, pnl in wallets_pnl:
        rows.append({
            'ethAddress': addr,
            'accountValue': '10000',
            'windowPerformances': [
                ['day',  {'pnl': '0', 'roi': '0', 'vlm': '0'}],
                ['week', {'pnl': '0', 'roi': '0', 'vlm': '0'}],
                ['allTime', {'pnl': str(pnl), 'roi': '0.1', 'vlm': '500000'}],
            ],
        })
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {'leaderboardRows': rows}
    return resp


def _clearinghouse_resp(positions):
    """Build a fake clearinghouseState JSON response.

    positions: list of dicts with keys: coin, szi, positionValue,
               and optionally liquidationPx (None is valid).
    """
    asset_positions = []
    for p in positions:
        pos = {
            'coin': p['coin'],
            'szi': str(p['szi']),
            'positionValue': str(abs(float(p.get('positionValue', p.get('szi', 0))))),
            'unrealizedPnl': str(p.get('unrealizedPnl', '0')),
            'entryPx': str(p.get('entryPx', '1000')),
            'leverage': p.get('leverage', {'type': 'cross', 'value': 5}),
            'marginUsed': str(p.get('marginUsed', '1000')),
            'liquidationPx': p.get('liquidationPx', None),  # may be null
        }
        asset_positions.append({'position': pos})

    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {'assetPositions': asset_positions}
    return resp


# ─────────────────────────────────────────────────────────────────────────────
# Leaderboard parsing
# ─────────────────────────────────────────────────────────────────────────────

class TestLeaderboardParsing:
    """_fetch_wallets: parse leaderboard, take top-N by allTime PnL."""

    def test_returns_top_n_addresses_by_pnl(self):
        """Top-N wallets are selected in descending PnL order."""
        wallets = [(f'0x{i:040x}', float(i * 1000)) for i in range(1, 60)]
        resp = _leaderboard_resp(wallets)
        c = HyperliquidSmartMoneyCollector(dry_run=True)
        with patch('requests.get', return_value=resp), \
             patch('time.sleep'):
            result = c._fetch_wallets()
        assert len(result) == TOP_N
        # Highest PnL first (wallet 59, 58, …)
        highest_pnl_addr = f'0x{59:040x}'
        assert result[0] == highest_pnl_addr

    def test_fallback_on_network_error(self, monkeypatch):
        """If leaderboard fetch raises, the configured wallets are returned."""
        monkeypatch.setenv('HL_SMART_MONEY_WALLETS', ' 0xaaa , 0xbbb,, ')
        c = HyperliquidSmartMoneyCollector(dry_run=True)
        with patch('requests.get', side_effect=Exception('network error')):
            result = c._fetch_wallets()
        assert result == ['0xaaa', '0xbbb']

    def test_fallback_on_empty_leaderboard(self, monkeypatch):
        """If leaderboardRows is empty, the configured wallets are returned."""
        monkeypatch.setenv('HL_SMART_MONEY_WALLETS', '0xaaa')
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {'leaderboardRows': []}
        c = HyperliquidSmartMoneyCollector(dry_run=True)
        with patch('requests.get', return_value=resp):
            result = c._fetch_wallets()
        assert result == ['0xaaa']

    def test_no_fallback_wallets_by_default(self, monkeypatch):
        """No wallet addresses ship in the code: unset means an empty list."""
        monkeypatch.delenv('HL_SMART_MONEY_WALLETS', raising=False)
        c = HyperliquidSmartMoneyCollector(dry_run=True)
        with patch('requests.get', side_effect=Exception('network error')):
            assert c._fetch_wallets() == []

    def test_fewer_than_top_n_available(self):
        """If fewer than TOP_N wallets in leaderboard, returns all available."""
        wallets = [(f'0x{i:040x}', float(i * 100)) for i in range(1, 10)]
        resp = _leaderboard_resp(wallets)
        c = HyperliquidSmartMoneyCollector(dry_run=True)
        with patch('requests.get', return_value=resp):
            result = c._fetch_wallets()
        assert len(result) == 9  # All available

    def test_alltime_pnl_used_not_weekly(self):
        """Wallets with high weekly PnL but low allTime PnL are ranked lower."""
        # Wallet A: allTime PnL = 1M (winner), weekly = 0
        # Wallet B: allTime PnL = 100 (loser), weekly = 999M
        # Manually add a huge weekly to B without touching allTime logic
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {
            'leaderboardRows': [
                {
                    'ethAddress': '0xAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA',
                    'accountValue': '1000000',
                    'windowPerformances': [
                        ['week', {'pnl': '0', 'roi': '0', 'vlm': '0'}],
                        ['allTime', {'pnl': '1000000', 'roi': '0.5', 'vlm': '5000000'}],
                    ],
                },
                {
                    'ethAddress': '0xBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB',
                    'accountValue': '100',
                    'windowPerformances': [
                        ['week', {'pnl': '999000000', 'roi': '9.9', 'vlm': '1000000'}],
                        ['allTime', {'pnl': '100', 'roi': '0.01', 'vlm': '100'}],
                    ],
                },
            ]
        }
        c = HyperliquidSmartMoneyCollector(dry_run=True)
        with patch('requests.get', return_value=resp):
            result = c._fetch_wallets()
        assert result[0] == '0xAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'


# ─────────────────────────────────────────────────────────────────────────────
# Per-wallet position fetch — szi sign + liquidationPx null safety
# ─────────────────────────────────────────────────────────────────────────────

class TestFetchWalletPositions:
    """_fetch_wallet_positions: parse positions from clearinghouseState."""

    def test_szi_positive_is_long(self):
        """szi > 0 → is_long = True."""
        resp = _clearinghouse_resp([
            {'coin': 'BTC', 'szi': '0.5', 'positionValue': '30000'},
        ])
        c = HyperliquidSmartMoneyCollector(dry_run=True)
        with patch('requests.post', return_value=resp):
            result = c._fetch_wallet_positions('0xABCD')
        assert len(result) == 1
        coin, pos_value, is_long = result[0]
        assert coin == 'BTC'
        assert is_long is True

    def test_szi_negative_is_short(self):
        """szi < 0 → is_long = False."""
        resp = _clearinghouse_resp([
            {'coin': 'ETH', 'szi': '-2.0', 'positionValue': '6000'},
        ])
        c = HyperliquidSmartMoneyCollector(dry_run=True)
        with patch('requests.post', return_value=resp):
            result = c._fetch_wallet_positions('0xABCD')
        assert len(result) == 1
        coin, pos_value, is_long = result[0]
        assert coin == 'ETH'
        assert is_long is False

    def test_szi_zero_is_skipped(self):
        """Flat position (szi = '0') is not included in results."""
        resp = _clearinghouse_resp([
            {'coin': 'SOL', 'szi': '0', 'positionValue': '0'},
        ])
        c = HyperliquidSmartMoneyCollector(dry_run=True)
        with patch('requests.post', return_value=resp):
            result = c._fetch_wallet_positions('0xABCD')
        assert result == []

    def test_liquidation_px_null_does_not_crash(self):
        """liquidationPx=null in the JSON should not raise any exception."""
        resp = _clearinghouse_resp([
            {'coin': 'BTC', 'szi': '1.0', 'positionValue': '60000', 'liquidationPx': None},
        ])
        c = HyperliquidSmartMoneyCollector(dry_run=True)
        with patch('requests.post', return_value=resp):
            # Must NOT raise
            result = c._fetch_wallet_positions('0xABCD')
        assert len(result) == 1

    def test_leverage_is_object_not_scalar(self):
        """leverage field is an object {type, value} — parsing must not crash."""
        resp = _clearinghouse_resp([
            {
                'coin': 'BTC',
                'szi': '0.1',
                'positionValue': '6000',
                'leverage': {'type': 'isolated', 'value': 10},
            },
        ])
        c = HyperliquidSmartMoneyCollector(dry_run=True)
        with patch('requests.post', return_value=resp):
            result = c._fetch_wallet_positions('0xABCD')
        assert len(result) == 1

    def test_multiple_positions_per_wallet(self):
        """A wallet with BTC long + ETH short returns both."""
        resp = _clearinghouse_resp([
            {'coin': 'BTC', 'szi': '0.5', 'positionValue': '30000'},
            {'coin': 'ETH', 'szi': '-5.0', 'positionValue': '15000'},
        ])
        c = HyperliquidSmartMoneyCollector(dry_run=True)
        with patch('requests.post', return_value=resp):
            result = c._fetch_wallet_positions('0xABCD')
        assert len(result) == 2
        coins = {r[0] for r in result}
        assert coins == {'BTC', 'ETH'}

    def test_empty_asset_positions(self):
        """Wallet with no positions returns empty list."""
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {'assetPositions': []}
        c = HyperliquidSmartMoneyCollector(dry_run=True)
        with patch('requests.post', return_value=resp):
            result = c._fetch_wallet_positions('0xABCD')
        assert result == []


# ─────────────────────────────────────────────────────────────────────────────
# Bias math — the core logic
# ─────────────────────────────────────────────────────────────────────────────

class TestBiasMath:
    """_compute_bias_rows: 25% cap, guards, bias formula."""

    def _make_positions(self, wallet_positions):
        """
        Build positions_by_asset dict from list of
        (wallet, coin, pos_value_abs, is_long) tuples.
        """
        positions_by_asset = defaultdict(list)
        for wallet, coin, pos_value, is_long in wallet_positions:
            positions_by_asset[coin].append((wallet, pos_value, is_long))
        return positions_by_asset

    # ── Boundary values ──────────────────────────────────────────────

    def test_all_long_bias_is_100(self):
        """All wallets long → bias = 100.0."""
        positions = [
            ('0xW1', 'BTC', 1_000_000.0, True),
            ('0xW2', 'BTC', 1_000_000.0, True),
            ('0xW3', 'BTC', 1_000_000.0, True),
        ]
        c = HyperliquidSmartMoneyCollector(dry_run=True)
        rows = c._compute_bias_rows('2026-06-30', self._make_positions(positions))
        assert len(rows) == 1
        assert rows[0]['value'] == 100.0

    def test_all_short_bias_is_0(self):
        """All wallets short → bias = 0.0."""
        positions = [
            ('0xW1', 'BTC', 1_000_000.0, False),
            ('0xW2', 'BTC', 1_000_000.0, False),
            ('0xW3', 'BTC', 1_000_000.0, False),
        ]
        c = HyperliquidSmartMoneyCollector(dry_run=True)
        rows = c._compute_bias_rows('2026-06-30', self._make_positions(positions))
        assert len(rows) == 1
        assert rows[0]['value'] == 0.0

    def test_balanced_bias_is_50(self):
        """Equal longs and shorts (3 each, equal size) → bias = 50.0."""
        positions = [
            ('0xW1', 'BTC', 1_000_000.0, True),
            ('0xW2', 'BTC', 1_000_000.0, True),
            ('0xW3', 'BTC', 1_000_000.0, True),
            ('0xW4', 'BTC', 1_000_000.0, False),
            ('0xW5', 'BTC', 1_000_000.0, False),
            ('0xW6', 'BTC', 1_000_000.0, False),
        ]
        c = HyperliquidSmartMoneyCollector(dry_run=True)
        rows = c._compute_bias_rows('2026-06-30', self._make_positions(positions))
        assert len(rows) == 1
        assert rows[0]['value'] == pytest.approx(50.0)

    # ── Guard: fewer than 3 distinct wallets ──────────────────────────

    def test_two_wallets_asset_skipped(self):
        """Only 2 distinct wallets → asset not written."""
        positions = [
            ('0xW1', 'BTC', 1_000_000.0, True),
            ('0xW2', 'BTC', 1_000_000.0, False),
        ]
        c = HyperliquidSmartMoneyCollector(dry_run=True)
        rows = c._compute_bias_rows('2026-06-30', self._make_positions(positions))
        assert rows == []

    def test_one_wallet_asset_skipped(self):
        """Only 1 distinct wallet → asset not written."""
        positions = [
            ('0xW1', 'BTC', 5_000_000.0, True),
        ]
        c = HyperliquidSmartMoneyCollector(dry_run=True)
        rows = c._compute_bias_rows('2026-06-30', self._make_positions(positions))
        assert rows == []

    def test_exactly_three_wallets_passes_guard(self):
        """Exactly 3 distinct wallets → asset IS written."""
        positions = [
            ('0xW1', 'BTC', 1_000_000.0, True),
            ('0xW2', 'BTC', 1_000_000.0, True),
            ('0xW3', 'BTC', 1_000_000.0, False),
        ]
        c = HyperliquidSmartMoneyCollector(dry_run=True)
        rows = c._compute_bias_rows('2026-06-30', self._make_positions(positions))
        assert len(rows) == 1

    # ── Guard: gross < $1M ────────────────────────────────────────────

    def test_gross_below_1m_asset_skipped(self):
        """Capped gross < $1M → asset not written."""
        positions = [
            ('0xW1', 'BTC', 200_000.0, True),
            ('0xW2', 'BTC', 200_000.0, True),
            ('0xW3', 'BTC', 200_000.0, False),
        ]
        # Total uncapped gross = 600K < 1M → skipped
        c = HyperliquidSmartMoneyCollector(dry_run=True)
        rows = c._compute_bias_rows('2026-06-30', self._make_positions(positions))
        assert rows == []

    def test_gross_exactly_1m_passes_guard(self):
        """Capped gross of exactly $1M is NOT skipped (strict < guard)."""
        # 3 wallets, each 333_334 USD → uncapped gross ≈ 1M
        # With 3 equal wallets, cap = 25% of uncapped ≈ 250K each → capped to 250K*3 = 750K
        # Hmm, let me think... with 3 wallets each at 500K, uncapped_gross = 1.5M
        # cap = 25% * 1.5M = 375K each (no capping needed since 500K > 375K)
        # Wait: cap per wallet = 0.25 * 1.5M = 375K, but each wallet only has 500K
        # so they'd be capped to 375K each → gross = 3 * 375K = 1.125M → passes
        # Better: 3 wallets each at exactly 400K → uncapped_gross = 1.2M
        # cap = 25% * 1.2M = 300K → each capped to 300K → gross = 900K → fails
        # Even better: 4 wallets each 250K → uncapped_gross = 1M,
        # cap = 250K each → gross = 1M exactly
        positions = [
            ('0xW1', 'BTC', 250_000.0, True),
            ('0xW2', 'BTC', 250_000.0, True),
            ('0xW3', 'BTC', 250_000.0, False),
            ('0xW4', 'BTC', 250_000.0, False),
        ]
        # uncapped_gross = 1M, cap = 250K each, no capping needed, gross = 1M
        c = HyperliquidSmartMoneyCollector(dry_run=True)
        rows = c._compute_bias_rows('2026-06-30', self._make_positions(positions))
        assert len(rows) == 1

    # ── 25% whale cap ─────────────────────────────────────────────────

    def test_25pct_whale_cap_applied(self):
        """One whale with >25% of gross gets capped; bias reflects capped values.

        Setup:
          Whale (W1): long BTC $9M  → would be 90% without cap
          W2: long BTC $500K
          W3: short BTC $500K
          uncapped_gross = $10M
          cap = 25% of $10M = $2.5M
          W1 capped to $2.5M long
          W2 uncapped $500K long   (< $2.5M cap)
          W3 uncapped $500K short  (< $2.5M cap)
          capped_longs = 2.5M + 0.5M = 3.0M
          capped_shorts = 0.5M
          gross = 3.5M, net = 2.5M
          bias_raw = 2.5/3.5 = 5/7 ≈ 0.7143
          bias = round((5/7 + 1) / 2 * 100, 1) = round(85.714…, 1) = 85.7
        """
        positions = [
            ('0xWhale', 'BTC', 9_000_000.0, True),
            ('0xW2',    'BTC',   500_000.0, True),
            ('0xW3',    'BTC',   500_000.0, False),
        ]
        c = HyperliquidSmartMoneyCollector(dry_run=True)
        rows = c._compute_bias_rows('2026-06-30', self._make_positions(positions))
        assert len(rows) == 1
        expected_bias = round((5 / 7 + 1) / 2 * 100, 1)
        assert rows[0]['value'] == pytest.approx(expected_bias, abs=0.05)

    def test_no_cap_when_all_wallets_below_25pct(self):
        """When each wallet is under 25% of gross, cap has no effect.

        3 wallets each with $1M long → each is 33% of $3M uncapped_gross.
        But 33% > 25%, so they ARE capped at 25% * $3M = $750K each.
        Use 4 wallets at $1M each: each is 25% of $4M → exactly at cap, still applies.
        Use 5 equal wallets instead: each 20% → no capping.

        5 × $1M long: uncapped_gross=$5M, cap=$1.25M each, no cap needed.
        bias = 100 (all long).
        """
        positions = [
            ('0xW1', 'ETH', 1_000_000.0, True),
            ('0xW2', 'ETH', 1_000_000.0, True),
            ('0xW3', 'ETH', 1_000_000.0, True),
            ('0xW4', 'ETH', 1_000_000.0, True),
            ('0xW5', 'ETH', 1_000_000.0, True),
        ]
        c = HyperliquidSmartMoneyCollector(dry_run=True)
        rows = c._compute_bias_rows('2026-06-30', self._make_positions(positions))
        assert len(rows) == 1
        assert rows[0]['value'] == 100.0  # all long

    # ── bias_raw → 0-100 mapping (hand-computed) ──────────────────────

    def test_bias_formula_hand_computed(self):
        """bias = round((bias_raw + 1) / 2 * 100, 1).

        6 wallets: 4 long × $1M, 2 short × $1M → net = 2M, gross = 6M.
        bias_raw = 2/6 = 1/3
        bias = round((1/3 + 1) / 2 * 100, 1) = round(66.666…, 1) = 66.7
        """
        positions = [
            ('0xW1', 'BTC', 1_000_000.0, True),
            ('0xW2', 'BTC', 1_000_000.0, True),
            ('0xW3', 'BTC', 1_000_000.0, True),
            ('0xW4', 'BTC', 1_000_000.0, True),
            ('0xW5', 'BTC', 1_000_000.0, False),
            ('0xW6', 'BTC', 1_000_000.0, False),
        ]
        c = HyperliquidSmartMoneyCollector(dry_run=True)
        rows = c._compute_bias_rows('2026-06-30', self._make_positions(positions))
        assert len(rows) == 1
        # With 6 equal wallets each is 1/6 of 6M = 1M → 16.7% of gross, under 25% cap
        expected = round((1 / 3 + 1) / 2 * 100, 1)  # 66.7
        assert rows[0]['value'] == pytest.approx(expected, abs=0.05)

    def test_bias_bounded_0_to_100(self):
        """bias value is always in [0.0, 100.0]."""
        # All short
        positions_short = [
            ('0xW1', 'ETH', 2_000_000.0, False),
            ('0xW2', 'ETH', 2_000_000.0, False),
            ('0xW3', 'ETH', 2_000_000.0, False),
        ]
        # All long
        positions_long = [
            ('0xW1', 'ETH', 2_000_000.0, True),
            ('0xW2', 'ETH', 2_000_000.0, True),
            ('0xW3', 'ETH', 2_000_000.0, True),
        ]
        c = HyperliquidSmartMoneyCollector(dry_run=True)
        rows_short = c._compute_bias_rows('2026-06-30', self._make_positions(positions_short))
        rows_long  = c._compute_bias_rows('2026-06-30', self._make_positions(positions_long))
        assert 0.0 <= rows_short[0]['value'] <= 100.0
        assert 0.0 <= rows_long[0]['value'] <= 100.0

    # ── Multiple assets ───────────────────────────────────────────────

    def test_multiple_qualifying_assets_all_written(self):
        """Two qualifying assets → two rows."""
        positions = [
            ('0xW1', 'BTC', 2_000_000.0, True),
            ('0xW2', 'BTC', 2_000_000.0, True),
            ('0xW3', 'BTC', 2_000_000.0, False),
            ('0xW1', 'ETH', 1_000_000.0, True),
            ('0xW2', 'ETH', 1_000_000.0, False),
            ('0xW3', 'ETH', 1_000_000.0, False),
        ]
        c = HyperliquidSmartMoneyCollector(dry_run=True)
        rows = c._compute_bias_rows('2026-06-30', self._make_positions(positions))
        assert len(rows) == 2
        assets = {r['asset'] for r in rows}
        assert assets == {'BTC', 'ETH'}

    def test_mixed_qualifying_and_not(self):
        """Asset with <3 wallets skipped; qualifying asset still written."""
        positions = [
            # BTC: 3 wallets, qualifies
            ('0xW1', 'BTC', 2_000_000.0, True),
            ('0xW2', 'BTC', 2_000_000.0, True),
            ('0xW3', 'BTC', 2_000_000.0, False),
            # SOL: only 2 wallets, skipped
            ('0xW1', 'SOL', 1_000_000.0, True),
            ('0xW2', 'SOL', 1_000_000.0, False),
        ]
        c = HyperliquidSmartMoneyCollector(dry_run=True)
        rows = c._compute_bias_rows('2026-06-30', self._make_positions(positions))
        assert len(rows) == 1
        assert rows[0]['asset'] == 'BTC'

    # ── Row shape ─────────────────────────────────────────────────────

    def test_row_has_required_keys(self):
        """Output row has ts, asset, metric, value, source."""
        positions = [
            ('0xW1', 'BTC', 2_000_000.0, True),
            ('0xW2', 'BTC', 2_000_000.0, True),
            ('0xW3', 'BTC', 2_000_000.0, False),
        ]
        c = HyperliquidSmartMoneyCollector(dry_run=True)
        rows = c._compute_bias_rows('2026-06-30', self._make_positions(positions))
        assert len(rows) == 1
        row = rows[0]
        for key in ('ts', 'asset', 'metric', 'value', 'source'):
            assert key in row

    def test_row_metric_is_smart_money_bias(self):
        positions = [
            ('0xW1', 'BTC', 2_000_000.0, True),
            ('0xW2', 'BTC', 2_000_000.0, True),
            ('0xW3', 'BTC', 2_000_000.0, False),
        ]
        c = HyperliquidSmartMoneyCollector(dry_run=True)
        rows = c._compute_bias_rows('2026-06-30', self._make_positions(positions))
        assert rows[0]['metric'] == 'SMART_MONEY_BIAS'

    def test_row_source_is_hyperliquid(self):
        positions = [
            ('0xW1', 'BTC', 2_000_000.0, True),
            ('0xW2', 'BTC', 2_000_000.0, True),
            ('0xW3', 'BTC', 2_000_000.0, False),
        ]
        c = HyperliquidSmartMoneyCollector(dry_run=True)
        rows = c._compute_bias_rows('2026-06-30', self._make_positions(positions))
        assert rows[0]['source'] == 'hyperliquid'

    def test_row_ts_preserved(self):
        positions = [
            ('0xW1', 'BTC', 2_000_000.0, True),
            ('0xW2', 'BTC', 2_000_000.0, True),
            ('0xW3', 'BTC', 2_000_000.0, False),
        ]
        c = HyperliquidSmartMoneyCollector(dry_run=True)
        rows = c._compute_bias_rows('2026-06-30', self._make_positions(positions))
        assert rows[0]['ts'] == '2026-06-30'


# ─────────────────────────────────────────────────────────────────────────────
# Per-wallet error isolation
# ─────────────────────────────────────────────────────────────────────────────

class TestPerWalletErrorIsolation:
    """One wallet raising an exception must not abort the run."""

    def test_one_bad_wallet_others_still_collected(self):
        """If wallet W2 returns HTTP 500, W1 and W3 data are still aggregated.

        Scenario:
          W1: long BTC $2M (succeeds)
          W2: raises RuntimeError (fails)
          W3: short BTC $1M (succeeds)
        → BTC has 2 wallets → below MIN_WALLETS → 0 rows
        → but the run completes without exception
        """
        w1_resp = _clearinghouse_resp([{'coin': 'BTC', 'szi': '1.0', 'positionValue': '2000000'}])
        w3_resp = _clearinghouse_resp([{'coin': 'BTC', 'szi': '-0.5', 'positionValue': '1000000'}])

        def post_side_effect(*args, **kwargs):
            body = kwargs.get('json', {})
            user = body.get('user', '')
            if user == '0xBad':
                raise RuntimeError('HTTP 500 error')
            if user == '0xW1':
                return w1_resp
            if user == '0xW3':
                return w3_resp
            raise ValueError(f'Unexpected user: {user}')

        wallets = ['0xW1', '0xBad', '0xW3']
        c = HyperliquidSmartMoneyCollector(dry_run=True)
        with patch('requests.post', side_effect=post_side_effect), \
             patch('time.sleep'):
            # Must not raise
            result = c._fetch_all_positions(wallets)

        # Both good wallets' data is present
        btc_positions = result.get('BTC', [])
        wallets_in_btc = {wp[0] for wp in btc_positions}
        assert '0xW1' in wallets_in_btc
        assert '0xW3' in wallets_in_btc
        assert '0xBad' not in wallets_in_btc

    def test_all_wallets_fail_returns_empty(self):
        """If all wallet fetches fail, positions_by_asset is empty."""
        def post_side_effect(*args, **kwargs):
            raise RuntimeError('all fail')

        wallets = ['0xW1', '0xW2', '0xW3']
        c = HyperliquidSmartMoneyCollector(dry_run=True)
        with patch('requests.post', side_effect=post_side_effect), \
             patch('time.sleep'):
            result = c._fetch_all_positions(wallets)

        assert dict(result) == {}

    def test_politeness_sleep_called_per_wallet(self):
        """A sleep is called once per wallet to avoid rate limiting."""
        good_resp = _clearinghouse_resp([])

        wallets = ['0xW1', '0xW2', '0xW3']
        c = HyperliquidSmartMoneyCollector(dry_run=True)
        with patch('requests.post', return_value=good_resp), \
             patch('time.sleep') as mock_sleep:
            c._fetch_all_positions(wallets)

        assert mock_sleep.call_count == len(wallets)


# ─────────────────────────────────────────────────────────────────────────────
# End-to-end collect() integration (no real network/DB)
# ─────────────────────────────────────────────────────────────────────────────

class TestCollectIntegration:
    """collect() wires leaderboard → positions → bias → upsert."""

    def _build_leaderboard_resp(self, n=5):
        """Return a leaderboard response with n wallets."""
        wallets = [(f'0x{i:040x}', float(i * 1000)) for i in range(1, n + 1)]
        return _leaderboard_resp(wallets)

    def test_no_qualifying_assets_upsert_not_called(self):
        """If all assets have <3 wallets, upsert_rows is NOT called."""
        # 5 wallets, each holds a DIFFERENT coin (1 wallet per coin)
        def post_side_effect(*args, **kwargs):
            user = kwargs.get('json', {}).get('user', '')
            idx = int(user.lstrip('0x') or 0, 16)
            resp = _clearinghouse_resp([
                {'coin': f'COIN{idx}', 'szi': '1.0', 'positionValue': '5000000'},
            ])
            return resp

        lb_resp = self._build_leaderboard_resp(n=5)
        c = HyperliquidSmartMoneyCollector(dry_run=True)
        with patch('requests.get', return_value=lb_resp), \
             patch('requests.post', side_effect=post_side_effect), \
             patch('time.sleep'), \
             patch.object(c, 'upsert_rows') as mock_upsert:
            c.collect()
        mock_upsert.assert_not_called()

    def test_qualifying_asset_rows_upserted(self):
        """When an asset qualifies, upsert_rows is called with correct table and conflict cols."""
        # 3 wallets all long BTC $5M each → bias = 100
        def post_side_effect(*args, **kwargs):
            return _clearinghouse_resp([
                {'coin': 'BTC', 'szi': '1.0', 'positionValue': '5000000'},
            ])

        # Use exactly 3 wallet addresses
        lb_resp = MagicMock()
        lb_resp.raise_for_status = MagicMock()
        lb_resp.json.return_value = {
            'leaderboardRows': [
                {
                    'ethAddress': f'0xWallet{i}',
                    'accountValue': '1000000',
                    'windowPerformances': [
                        ['allTime', {'pnl': str(i * 1000), 'roi': '0.1', 'vlm': '0'}],
                    ],
                }
                for i in range(3)
            ]
        }
        c = HyperliquidSmartMoneyCollector(dry_run=True)
        with patch('requests.get', return_value=lb_resp), \
             patch('requests.post', side_effect=post_side_effect), \
             patch('time.sleep'), \
             patch.object(c, 'upsert_rows') as mock_upsert:
            c.collect()

        assert mock_upsert.called
        args = mock_upsert.call_args
        table = args.args[0]
        rows = args.args[1]
        conflict_cols = args.kwargs.get('conflict_cols') or args.args[2]
        assert table == 'quant.onchain'
        assert any(r['metric'] == 'SMART_MONEY_BIAS' for r in rows)
        assert set(conflict_cols) == {'ts', 'asset', 'metric'}

    def test_collect_uses_fallback_when_leaderboard_fails(self, monkeypatch):
        """If leaderboard fetch fails, collect() still runs with the configured wallets."""
        wallets = ['0x' + 'a' * 40, '0x' + 'b' * 40]
        monkeypatch.setenv('HL_SMART_MONEY_WALLETS', ','.join(wallets))
        def get_side_effect(*args, **kwargs):
            raise ConnectionError('leaderboard unreachable')

        call_count = {'n': 0}

        def post_side_effect(*args, **kwargs):
            call_count['n'] += 1
            return _clearinghouse_resp([])  # empty positions

        c = HyperliquidSmartMoneyCollector(dry_run=True)
        with patch('requests.get', side_effect=get_side_effect), \
             patch('requests.post', side_effect=post_side_effect), \
             patch('time.sleep'), \
             patch.object(c, 'upsert_rows'):
            c.collect()  # Must not raise

        # Should have attempted to fetch positions for each configured wallet
        assert call_count['n'] == len(wallets)

    def test_collect_skips_with_warning_when_no_wallets(self, monkeypatch, caplog):
        """Leaderboard down and nothing configured: warn, fetch nothing, write nothing."""
        monkeypatch.delenv('HL_SMART_MONEY_WALLETS', raising=False)
        c = HyperliquidSmartMoneyCollector(dry_run=True)
        with patch('requests.get', side_effect=ConnectionError('leaderboard unreachable')), \
             patch('requests.post') as mock_post, \
             patch('time.sleep'), \
             patch.object(c, 'upsert_rows') as mock_upsert, \
             caplog.at_level('WARNING'):
            c.collect()
        assert not mock_post.called
        assert not mock_upsert.called
        assert 'HL_SMART_MONEY_WALLETS is empty' in caplog.text
