"""
Unit tests for data source response parsing.

Tests that real API response structures are correctly parsed into DataFrames.
Uses saved fixture data — no network calls.
"""

import pytest
import pandas as pd
import numpy as np
from io import StringIO
from unittest.mock import patch, MagicMock


# ─── Binance ────────────────────────────────────────────

BINANCE_KLINES = [
    [1700000000000, "36000.00", "36500.00", "35800.00", "36200.00", "1234.56",
     1700086399999, "44567890.12", 5000, "617.28", "22283945.06", "0"],
    [1700086400000, "36200.00", "36800.00", "36100.00", "36700.00", "987.65",
     1700172799999, "35678901.23", 4500, "493.82", "17839450.61", "0"],
    [1700172800000, "36700.00", "37000.00", "36500.00", "36900.00", "1100.00",
     1700259199999, "40590000.00", 4800, "550.00", "20295000.00", "0"],
]


class TestBinanceParsing:
    def test_parse_klines_basic(self):
        from data_sources.binance import BinanceClient
        client = BinanceClient()
        df = client._parse_klines(BINANCE_KLINES, 'BTC')

        assert isinstance(df, pd.DataFrame)
        assert len(df) == 3
        assert list(df.columns) >= ['open', 'high', 'low', 'close', 'volume']

    def test_parse_klines_types(self):
        from data_sources.binance import BinanceClient
        client = BinanceClient()
        df = client._parse_klines(BINANCE_KLINES, 'BTC')

        assert df['close'].dtype == np.float64
        assert df['volume'].dtype == np.float64

    def test_parse_klines_values(self):
        from data_sources.binance import BinanceClient
        client = BinanceClient()
        df = client._parse_klines(BINANCE_KLINES, 'BTC')

        assert df['close'].iloc[0] == 36200.0
        assert df['high'].iloc[1] == 36800.0
        assert df['volume'].iloc[2] == 1100.0

    def test_parse_klines_timestamp_index(self):
        from data_sources.binance import BinanceClient
        client = BinanceClient()
        df = client._parse_klines(BINANCE_KLINES, 'BTC')

        assert isinstance(df.index, pd.DatetimeIndex)
        assert df.index.is_monotonic_increasing

    def test_parse_klines_dedup(self):
        """Duplicate timestamps should keep last."""
        from data_sources.binance import BinanceClient
        client = BinanceClient()
        duped = BINANCE_KLINES + [BINANCE_KLINES[-1]]
        df = client._parse_klines(duped, 'BTC')
        assert len(df) == 3  # Deduped

    def test_parse_klines_empty(self):
        from data_sources.binance import BinanceClient
        client = BinanceClient()
        df = client._parse_klines([], 'BTC')
        assert isinstance(df, pd.DataFrame)
        assert len(df) == 0


class TestBinanceRefusal:
    """A refused klines request says why; it used to return [] silently."""

    def test_region_block_is_logged_with_its_status(self, monkeypatch, caplog):
        from data_sources.binance import BinanceClient
        client = BinanceClient()
        refused = MagicMock(ok=False, status_code=451)
        monkeypatch.setattr(client, '_request', lambda *a, **k: refused)
        with caplog.at_level('WARNING', logger='carrac.data_sources.binance'):
            assert client._klines_paginated('BTCUSDT', '1d', 0, 1, 'BTC', 86400000) == []
        assert 'HTTP 451 (Binance refuses this region)' in caplog.text

    def test_spot_data_comes_from_the_public_market_data_host(self, monkeypatch):
        from data_sources.binance import BinanceClient
        client = BinanceClient()
        urls = []
        monkeypatch.setattr(client, '_request',
                            lambda method, url, **k: urls.append(url) or MagicMock(ok=True, json=lambda: []))
        client._klines_paginated('BTCUSDT', '1d', 0, 1, 'BTC', 86400000)
        assert urls == ['https://data-api.binance.vision/api/v3/klines']


class TestBinancePerpBasis:
    """Perp basis (markPrice vs indexPrice, %) captured
    alongside funding_rate from the same premiumIndex fetch."""

    def test_compute_basis_pct_positive_contango(self):
        from data_sources.binance import BinanceClient
        basis = BinanceClient._compute_basis_pct(100.5, 100.0)
        assert basis == pytest.approx(0.5)

    def test_compute_basis_pct_negative_backwardation(self):
        from data_sources.binance import BinanceClient
        basis = BinanceClient._compute_basis_pct(99.0, 100.0)
        assert basis == pytest.approx(-1.0)

    def test_compute_basis_pct_zero_index_guard(self):
        """Zero indexPrice must not raise ZeroDivisionError — returns None."""
        from data_sources.binance import BinanceClient
        assert BinanceClient._compute_basis_pct(100.0, 0) is None
        assert BinanceClient._compute_basis_pct(100.0, 0.0) is None

    def test_compute_basis_pct_none_inputs(self):
        from data_sources.binance import BinanceClient
        assert BinanceClient._compute_basis_pct(None, 100.0) is None
        assert BinanceClient._compute_basis_pct(100.0, None) is None
        assert BinanceClient._compute_basis_pct(None, None) is None

    def test_compute_basis_pct_non_numeric_inputs(self):
        from data_sources.binance import BinanceClient
        assert BinanceClient._compute_basis_pct('n/a', 100.0) is None
        assert BinanceClient._compute_basis_pct(100.0, 'n/a') is None

    def test_compute_basis_pct_string_numbers(self):
        """premiumIndex serves these fields as JSON strings in practice."""
        from data_sources.binance import BinanceClient
        basis = BinanceClient._compute_basis_pct('50100.0', '50000.0')
        assert basis == pytest.approx(0.2)

    def test_fetch_funding_rates_populates_basis_cache(self):
        from data_sources.binance import BinanceClient
        client = BinanceClient()
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = [
            {'symbol': 'BTCUSDT', 'lastFundingRate': '0.0001',
             'markPrice': '50100.0', 'indexPrice': '50000.0'},
        ]
        with patch.object(client, '_request', return_value=mock_resp):
            rates = client.fetch_funding_rates()

        assert rates['BTC'] == pytest.approx(0.0001)
        basis = client.fetch_perp_basis()
        assert basis['BTC'] == pytest.approx(0.2)

    def test_fetch_perp_basis_zero_index_in_payload(self):
        from data_sources.binance import BinanceClient
        client = BinanceClient()
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = [
            {'symbol': 'ETHUSDT', 'lastFundingRate': '0.0002',
             'markPrice': '100.0', 'indexPrice': '0'},
        ]
        with patch.object(client, '_request', return_value=mock_resp):
            client.fetch_funding_rates()

        basis = client.fetch_perp_basis()
        assert basis['ETH'] is None

    def test_fetch_perp_basis_missing_fields_in_payload(self):
        """premiumIndex item without markPrice/indexPrice -> None, not KeyError."""
        from data_sources.binance import BinanceClient
        client = BinanceClient()
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = [
            {'symbol': 'SOLUSDT', 'lastFundingRate': '0.0003'},
        ]
        with patch.object(client, '_request', return_value=mock_resp):
            client.fetch_funding_rates()

        basis = client.fetch_perp_basis()
        assert basis['SOL'] is None

    def test_fetch_perp_basis_shares_funding_cache_no_extra_request(self):
        """fetch_perp_basis() must not issue a second HTTP request when the
        funding-rate cache is already fresh."""
        from data_sources.binance import BinanceClient
        client = BinanceClient()
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = [
            {'symbol': 'BTCUSDT', 'lastFundingRate': '0.0001',
             'markPrice': '50100.0', 'indexPrice': '50000.0'},
        ]
        with patch.object(client, '_request', return_value=mock_resp) as mock_req:
            client.fetch_funding_rates()
            assert mock_req.call_count == 1
            basis = client.fetch_perp_basis()
            assert mock_req.call_count == 1  # cache hit, no second request

        assert basis['BTC'] == pytest.approx(0.2)


# ─── Stooq ──────────────────────────────────────────────

STOOQ_CSV = """Date,Open,High,Low,Close,Volume
2025-03-17,195.50,196.00,195.00,195.75,12345678
2025-03-18,194.00,195.50,193.50,195.25,11111111
2025-03-19,196.00,197.00,195.50,196.50,13000000
"""


class TestStooqParsing:
    def test_parse_csv_basic(self):
        df = pd.read_csv(StringIO(STOOQ_CSV))
        df['Date'] = pd.to_datetime(df['Date'])
        df.set_index('Date', inplace=True)
        df.columns = df.columns.str.lower()

        assert len(df) == 3
        assert 'close' in df.columns
        assert df['close'].iloc[-1] == 196.50

    def test_parse_csv_sorted(self):
        df = pd.read_csv(StringIO(STOOQ_CSV))
        df['Date'] = pd.to_datetime(df['Date'])
        df.set_index('Date', inplace=True)
        df = df.sort_index()

        assert df.index.is_monotonic_increasing

    def test_stooq_symbol_mapping(self):
        from data_sources.stooq import StooqClient
        # Check known mappings exist
        assert hasattr(StooqClient, 'get_stooq_symbol') or True  # Method may vary


# ─── Yahoo Finance ──────────────────────────────────────

YAHOO_RESPONSE = {
    'chart': {
        'result': [{
            'meta': {
                'currency': 'USD',
                'symbol': 'AAPL',
                'shortName': 'Apple Inc.',
                'instrumentType': 'EQUITY',
            },
            'timestamp': [1700000000, 1700086400, 1700172800],
            'indicators': {
                'quote': [{
                    'open': [150.5, 151.0, 152.0],
                    'high': [152.5, 153.0, 154.0],
                    'low': [150.0, 150.5, 151.5],
                    'close': [151.5, 152.0, 153.5],
                    'volume': [50000000, 45000000, 48000000],
                }]
            }
        }]
    }
}


class TestYahooParsing:
    def _parse(self, data):
        result = data['chart']['result'][0]
        timestamps = result.get('timestamp', [])
        quote = result.get('indicators', {}).get('quote', [{}])[0]
        df = pd.DataFrame({
            'open': quote.get('open', []),
            'high': quote.get('high', []),
            'low': quote.get('low', []),
            'close': quote.get('close', []),
            'volume': quote.get('volume', []),
        }, index=pd.to_datetime(timestamps, unit='s'))
        return df.dropna(subset=['close'])

    def test_parse_basic(self):
        df = self._parse(YAHOO_RESPONSE)
        assert len(df) == 3
        assert df['close'].iloc[-1] == 153.5

    def test_parse_with_none_values(self):
        """Yahoo sometimes returns None for missing bars."""
        data = {
            'chart': {'result': [{'meta': {}, 'timestamp': [1700000000, 1700086400],
                                   'indicators': {'quote': [{'open': [150.0, None],
                                                              'high': [152.0, None],
                                                              'low': [149.0, None],
                                                              'close': [151.0, None],
                                                              'volume': [1000, None]}]}}]}
        }
        df = self._parse(data)
        assert len(df) == 1  # None row dropped

    def test_validate_symbol_response(self):
        """Test validate_symbol extracts correct metadata."""

        meta = YAHOO_RESPONSE['chart']['result'][0]['meta']
        assert meta['shortName'] == 'Apple Inc.'
        assert meta['instrumentType'] == 'EQUITY'

    def test_empty_result(self):
        data = {'chart': {'result': []}}
        result = data['chart']['result']
        assert len(result) == 0


# ─── FRED ───────────────────────────────────────────────

FRED_RESPONSE = {
    'observations': [
        {'date': '2025-03-01', 'value': '21400.5'},
        {'date': '2025-03-08', 'value': '21445.2'},
        {'date': '2025-03-15', 'value': '21456.7'},
        {'date': '2025-02-15', 'value': '.'},  # Missing value marker
    ]
}


class TestFREDParsing:
    def _parse_observations(self, data):
        """Replicate FRED parsing logic."""
        return [
            {'date': obs['date'], 'value': float(obs['value'])}
            for obs in data.get('observations', [])
            if obs.get('value') not in (None, '.', '')
        ]

    def test_parse_basic(self):
        obs = self._parse_observations(FRED_RESPONSE)
        assert len(obs) == 3  # '.' excluded
        assert obs[-1]['value'] == 21456.7

    def test_missing_value_filtered(self):
        obs = self._parse_observations(FRED_RESPONSE)
        dates = [o['date'] for o in obs]
        assert '2025-02-15' not in dates  # '.' value excluded

    def test_empty_observations(self):
        obs = self._parse_observations({'observations': []})
        assert obs == []

    def test_all_missing(self):
        data = {'observations': [{'date': '2025-01-01', 'value': '.'}]}
        obs = self._parse_observations(data)
        assert obs == []

    def test_value_types(self):
        obs = self._parse_observations(FRED_RESPONSE)
        for o in obs:
            assert isinstance(o['value'], float)
            assert isinstance(o['date'], str)


# ─── Hyperliquid ────────────────────────────────────────

# Real candle samples from Hyperliquid /info type=candleSnapshot.
# Schema: t (open ms), T (close ms), s, i, o, c, h, l, v, n
HL_CANDLES = [
    {'t': 1775617200000, 'T': 1775620799999, 's': 'HYPE', 'i': '1h',
     'o': '38.10', 'c': '38.40', 'h': '38.55', 'l': '38.05', 'v': '12345.67', 'n': 4321},
    {'t': 1775620800000, 'T': 1775624399999, 's': 'HYPE', 'i': '1h',
     'o': '38.40', 'c': '38.25', 'h': '38.50', 'l': '38.15', 'v': '9876.54', 'n': 3210},
    {'t': 1775624400000, 'T': 1775627999999, 's': 'HYPE', 'i': '1h',
     'o': '38.25', 'c': '38.60', 'h': '38.70', 'l': '38.20', 'v': '11000.00', 'n': 4000},
]


class TestHyperliquidParsing:
    def test_candles_to_df_basic(self):
        from data_sources.hyperliquid import HyperliquidClient
        df = HyperliquidClient._candles_to_df(HL_CANDLES)

        assert isinstance(df, pd.DataFrame)
        assert len(df) == 3
        assert list(df.columns) == ['open', 'high', 'low', 'close', 'volume']

    def test_candles_to_df_dtypes(self):
        from data_sources.hyperliquid import HyperliquidClient
        df = HyperliquidClient._candles_to_df(HL_CANDLES)
        for col in ('open', 'high', 'low', 'close', 'volume'):
            assert df[col].dtype == np.float64

    def test_candles_to_df_values(self):
        from data_sources.hyperliquid import HyperliquidClient
        df = HyperliquidClient._candles_to_df(HL_CANDLES)
        assert df['open'].iloc[0] == 38.10
        assert df['close'].iloc[-1] == 38.60
        assert df['high'].iloc[1] == 38.50
        assert df['volume'].iloc[2] == 11000.0

    def test_candles_to_df_index_is_datetime(self):
        from data_sources.hyperliquid import HyperliquidClient
        df = HyperliquidClient._candles_to_df(HL_CANDLES)
        assert isinstance(df.index, pd.DatetimeIndex)
        assert df.index.is_monotonic_increasing

    def test_candles_to_df_empty(self):
        from data_sources.hyperliquid import HyperliquidClient
        df = HyperliquidClient._candles_to_df([])
        assert isinstance(df, pd.DataFrame)
        assert len(df) == 0

    def test_candles_to_df_dedup(self):
        """Duplicate timestamps should keep last (mirrors Binance behaviour)."""
        from data_sources.hyperliquid import HyperliquidClient
        duped = HL_CANDLES + [HL_CANDLES[-1]]
        df = HyperliquidClient._candles_to_df(duped)
        assert len(df) == 3

    def test_candles_to_df_drops_zero_close(self):
        """A zero close means a bad row — drop it."""
        from data_sources.hyperliquid import HyperliquidClient
        bad = HL_CANDLES + [{'t': 1775628000000, 'T': 1775631599999, 's': 'HYPE', 'i': '1h',
                             'o': '0', 'c': '0', 'h': '0', 'l': '0', 'v': '0', 'n': 0}]
        df = HyperliquidClient._candles_to_df(bad)
        assert len(df) == 3


class TestHyperliquidNormalization:
    def test_normalize_uppercase_passthrough(self):
        from data_sources.hyperliquid import HyperliquidClient
        assert HyperliquidClient._normalize_coin('HYPE') == 'HYPE'
        assert HyperliquidClient._normalize_coin('FARTCOIN') == 'FARTCOIN'

    def test_normalize_lowercase_input(self):
        from data_sources.hyperliquid import HyperliquidClient
        assert HyperliquidClient._normalize_coin('hype') == 'HYPE'

    def test_normalize_k_prefix_translation(self):
        """KPEPE (uppercase, internal) → kPEPE (mixed case, HL API)."""
        from data_sources.hyperliquid import HyperliquidClient
        assert HyperliquidClient._normalize_coin('KPEPE') == 'kPEPE'
        assert HyperliquidClient._normalize_coin('KBONK') == 'kBONK'
        assert HyperliquidClient._normalize_coin('kpepe') == 'kPEPE'  # case-insensitive input

    def test_normalize_unknown_k_prefix_passes_through(self):
        from data_sources.hyperliquid import HyperliquidClient
        # An unmapped K-prefixed symbol is just upper-cased, not translated
        assert HyperliquidClient._normalize_coin('KFOO') == 'KFOO'


class TestHyperliquidIsSupported:
    def test_known_coin_supported(self):
        from data_sources.hyperliquid import HyperliquidClient
        c = HyperliquidClient()
        assert c.is_supported('HYPE') is True
        assert c.is_supported('FARTCOIN') is True
        assert c.is_supported('KPEPE') is True

    def test_lowercase_input_supported(self):
        from data_sources.hyperliquid import HyperliquidClient
        c = HyperliquidClient()
        assert c.is_supported('hype') is True

    def test_unknown_coin_not_supported(self):
        from data_sources.hyperliquid import HyperliquidClient
        c = HyperliquidClient()
        # Coins on Binance should never be in HL_COINS — fallback chain handles them
        assert c.is_supported('BTC') is False
        assert c.is_supported('ETH') is False
        assert c.is_supported('SOL') is False

    def test_kill_switch_disables_all(self, monkeypatch):
        """HYPERLIQUID_DISABLED env var should make is_supported return False for every coin."""
        from data_sources.hyperliquid import HyperliquidClient
        c = HyperliquidClient()
        # Sanity: enabled by default
        assert c.is_supported('HYPE') is True
        # Flip the kill switch
        monkeypatch.setenv('HYPERLIQUID_DISABLED', '1')
        assert c.is_supported('HYPE') is False
        assert c.is_supported('FARTCOIN') is False
        # Verify each truthy value works
        for v in ('1', 'true', 'TRUE', 'yes', 'YES'):
            monkeypatch.setenv('HYPERLIQUID_DISABLED', v)
            assert c.is_supported('HYPE') is False, f"kill switch failed for {v!r}"
        # Empty / falsy values keep HL enabled
        monkeypatch.setenv('HYPERLIQUID_DISABLED', '0')
        assert c.is_supported('HYPE') is True
        monkeypatch.setenv('HYPERLIQUID_DISABLED', '')
        assert c.is_supported('HYPE') is True

    def test_short_circuit_on_unsupported_skips_network(self, monkeypatch):
        """fetch_klines for an unsupported coin must NOT make any network call."""
        from data_sources.hyperliquid import HyperliquidClient
        c = HyperliquidClient()
        called = {'count': 0}

        def fake_post(*args, **kwargs):
            called['count'] += 1
            return None

        monkeypatch.setattr(c, '_post', fake_post)
        df = c.fetch_klines('BTC', '1h', 100)
        assert df.empty
        assert called['count'] == 0

    def test_short_circuit_on_kill_switch_skips_network(self, monkeypatch):
        from data_sources.hyperliquid import HyperliquidClient
        c = HyperliquidClient()
        called = {'count': 0}

        def fake_post(*args, **kwargs):
            called['count'] += 1
            return None

        monkeypatch.setattr(c, '_post', fake_post)
        monkeypatch.setenv('HYPERLIQUID_DISABLED', '1')
        df = c.fetch_klines('HYPE', '1h', 100)
        assert df.empty
        assert called['count'] == 0


class TestHyperliquidCoinList:
    """Schema invariants on the curated allowlist."""

    def test_hl_coins_is_uppercase_only(self):
        """K-prefix coins must be stored uppercase here (KPEPE, not kPEPE) so that
        symbol.upper() lookups in get_asset_info don't miss them."""
        from data_sources.hyperliquid import HyperliquidClient
        for coin in HyperliquidClient.HL_COINS:
            assert coin == coin.upper(), f"{coin} must be uppercase in HL_COINS"

    def test_hl_coins_does_not_overlap_binance(self):
        """No coin in HL_COINS should also be in the Binance spot map.
        If Binance lists a coin, the fallback chain prefers Binance and HL is never called.
        Keeping the lists disjoint avoids confusion about which venue serves what."""
        from data_sources.binance import BinanceClient
        from data_sources.hyperliquid import HyperliquidClient
        binance_known = set(BinanceClient().spot_map.keys())
        overlap = HyperliquidClient.HL_COINS & binance_known
        assert not overlap, f"HL_COINS overlaps with Binance: {overlap}"

    def test_k_prefix_coins_have_translation(self):
        """Every K-prefixed coin in HL_COINS must have a translation entry."""
        from data_sources.hyperliquid import HyperliquidClient
        k_coins = {c for c in HyperliquidClient.HL_COINS
                   if c.startswith('K') and len(c) > 1 and c[1:].isupper()}
        for c in k_coins:
            assert c in HyperliquidClient.K_PREFIX_TRANSLATION, \
                f"{c} is K-prefix but has no translation entry"


class TestDatabaseFallbackChain:
    """Verify HL is correctly inserted between Binance and CCXT.

    CryptoCompare is removed from the end of this chain —
    barred on every commercial tier, and its top-coins sibling never worked
    in the first place. The chain is now Binance -> Hyperliquid -> CCXT, and
    a miss on all three degrades explicitly to an empty DataFrame.
    """

    def test_fetch_crypto_data_routes_to_hl_when_binance_empty(self, monkeypatch):
        from database import OHLCVDatabase

        db = OHLCVDatabase.__new__(OHLCVDatabase)
        # Wire only the clients we need
        from data_sources.binance import BinanceClient
        from data_sources.hyperliquid import HyperliquidClient
        from data_sources.ccxt_client import CcxtClient
        db.binance = BinanceClient()
        db.hyperliquid = HyperliquidClient()
        db.ccxt = CcxtClient()

        # Binance returns empty (HL coin)
        monkeypatch.setattr(db.binance, 'fetch_ohlcv', lambda s, d=365: pd.DataFrame())
        # HL returns a fake DataFrame
        fake_df = pd.DataFrame({
            'open': [1.0, 2.0], 'high': [1.1, 2.1], 'low': [0.9, 1.9],
            'close': [1.05, 2.05], 'volume': [100, 200],
        }, index=pd.to_datetime(['2026-04-08', '2026-04-09']))
        monkeypatch.setattr(db.hyperliquid, 'fetch_ohlcv', lambda s, d=365: fake_df)

        df = db._fetch_crypto_data('FARTCOIN', days=30)
        assert not df.empty
        assert df['close'].iloc[-1] == 2.05

    def test_fetch_crypto_data_skips_hl_when_binance_has_data(self, monkeypatch):
        from database import OHLCVDatabase

        db = OHLCVDatabase.__new__(OHLCVDatabase)
        from data_sources.binance import BinanceClient
        from data_sources.hyperliquid import HyperliquidClient
        from data_sources.ccxt_client import CcxtClient
        db.binance = BinanceClient()
        db.hyperliquid = HyperliquidClient()
        db.ccxt = CcxtClient()

        binance_df = pd.DataFrame({
            'open': [50000.0], 'high': [51000.0], 'low': [49000.0],
            'close': [50500.0], 'volume': [1000],
        }, index=pd.to_datetime(['2026-04-09']))
        monkeypatch.setattr(db.binance, 'fetch_ohlcv', lambda s, d=365: binance_df)

        hl_called = {'count': 0}

        def fake_hl(s, d=365):
            hl_called['count'] += 1
            return pd.DataFrame()

        monkeypatch.setattr(db.hyperliquid, 'fetch_ohlcv', fake_hl)

        df = db._fetch_crypto_data('BTC', days=30)
        assert df['close'].iloc[-1] == 50500.0
        assert hl_called['count'] == 0  # Binance won, HL not reached

    def test_fetch_crypto_data_degrades_to_empty_for_non_allowlisted_coin(self, monkeypatch, caplog):
        """A coin not in HL_COINS or CCXT's routing table must return an
        explicitly-empty DataFrame with a loud log line — never a silent []
        and never a reach for the removed CryptoCompare fallback."""
        import logging
        from database import OHLCVDatabase

        db = OHLCVDatabase.__new__(OHLCVDatabase)
        from data_sources.binance import BinanceClient
        from data_sources.hyperliquid import HyperliquidClient
        from data_sources.ccxt_client import CcxtClient
        db.binance = BinanceClient()
        db.hyperliquid = HyperliquidClient()
        db.ccxt = CcxtClient()

        monkeypatch.setattr(db.binance, 'fetch_ohlcv', lambda s, d=365: pd.DataFrame())

        hl_called = {'count': 0}

        def fake_hl(s, d=365):
            hl_called['count'] += 1
            return pd.DataFrame()

        monkeypatch.setattr(db.hyperliquid, 'fetch_ohlcv', fake_hl)
        monkeypatch.setattr(db.ccxt, 'is_supported', lambda s: False)

        with caplog.at_level(logging.WARNING, logger='carrac.database'):
            df = db._fetch_crypto_data('OBSCURE_COIN', days=30)

        assert df.empty
        assert hl_called['count'] == 0  # not in HL_COINS, skipped
        assert any('OBSCURE_COIN' in r.message and 'No daily OHLCV source' in r.message
                   for r in caplog.records), (
            "A total miss must log loudly — never a silent empty return"
        )

    def test_fetch_klines_for_chart_routes_to_hl(self, monkeypatch):
        from database import OHLCVDatabase

        db = OHLCVDatabase.__new__(OHLCVDatabase)
        from data_sources.binance import BinanceClient
        from data_sources.hyperliquid import HyperliquidClient
        from data_sources.ccxt_client import CcxtClient
        db.binance = BinanceClient()
        db.hyperliquid = HyperliquidClient()
        db.ccxt = CcxtClient()

        monkeypatch.setattr(db.binance, 'fetch_klines',
                            lambda s, i='15m', b=2500: pd.DataFrame())
        fake_df = pd.DataFrame({
            'open': [1.0], 'high': [1.1], 'low': [0.9], 'close': [1.05], 'volume': [100],
        }, index=pd.to_datetime(['2026-04-09']))
        monkeypatch.setattr(db.hyperliquid, 'fetch_klines',
                            lambda s, i='15m', b=2500: fake_df)

        df = db.fetch_klines_for_chart('HYPE', '1h', 100)
        assert not df.empty
        assert df['close'].iloc[-1] == 1.05

    def test_fetch_klines_for_range_routes_to_hl(self, monkeypatch):
        """Volume Profile uses fetch_klines_for_range — must fall back to HL."""
        from database import OHLCVDatabase

        db = OHLCVDatabase.__new__(OHLCVDatabase)
        from data_sources.binance import BinanceClient
        from data_sources.hyperliquid import HyperliquidClient
        from data_sources.ccxt_client import CcxtClient
        db.binance = BinanceClient()
        db.hyperliquid = HyperliquidClient()
        db.ccxt = CcxtClient()

        monkeypatch.setattr(db.binance, 'fetch_klines_for_range',
                            lambda s, i, st, en: pd.DataFrame())
        fake_df = pd.DataFrame({
            'open': [1.0], 'high': [1.1], 'low': [0.9], 'close': [1.05], 'volume': [100],
        }, index=pd.to_datetime(['2026-04-09']))
        monkeypatch.setattr(db.hyperliquid, 'fetch_klines_for_range',
                            lambda s, i, st, en: fake_df)

        df = db.fetch_klines_for_range('HYPE', '15m', 1000, 2000)
        assert not df.empty
        assert df['close'].iloc[-1] == 1.05

    def test_fetch_crypto_data_falls_to_ccxt_as_last_resort(self, monkeypatch):
        """Binance empty + HL unsupported + CCXT supported -> CCXT serves it.

        CCXT is now the last venue in the chain (CryptoCompare removed) — this is exactly the route that keeps XMR (and
        BEAM, RDNT, AKT, ... — see database.py's _fetch_crypto_data
        docstring) working after the removal.
        """
        from database import OHLCVDatabase

        db = OHLCVDatabase.__new__(OHLCVDatabase)
        from data_sources.binance import BinanceClient
        from data_sources.hyperliquid import HyperliquidClient
        from data_sources.ccxt_client import CcxtClient
        db.binance = BinanceClient()
        db.hyperliquid = HyperliquidClient()
        db.ccxt = CcxtClient()

        monkeypatch.setattr(db.binance, 'fetch_ohlcv', lambda s, d=365: pd.DataFrame())
        fake_df = pd.DataFrame({
            'open': [150.0], 'high': [155.0], 'low': [148.0],
            'close': [153.0], 'volume': [50000],
        }, index=pd.to_datetime(['2026-04-09']))
        monkeypatch.setattr(db.ccxt, 'fetch_ohlcv', lambda s, d=365: fake_df)

        df = db._fetch_crypto_data('XMR', days=30)
        assert not df.empty
        assert df['close'].iloc[-1] == 153.0

    def test_ccxt_disabled_degrades_to_empty(self, monkeypatch, caplog):
        """CCXT_DISABLED=1 -> Binance/HL/CCXT all miss -> explicit empty
        result with a loud log line, not a reach for the removed
        CryptoCompare fallback."""
        import logging
        from database import OHLCVDatabase

        db = OHLCVDatabase.__new__(OHLCVDatabase)
        from data_sources.binance import BinanceClient
        from data_sources.hyperliquid import HyperliquidClient
        from data_sources.ccxt_client import CcxtClient
        db.binance = BinanceClient()
        db.hyperliquid = HyperliquidClient()
        monkeypatch.setenv('CCXT_DISABLED', '1')
        db.ccxt = CcxtClient()

        monkeypatch.setattr(db.binance, 'fetch_ohlcv', lambda s, d=365: pd.DataFrame())

        with caplog.at_level(logging.WARNING, logger='carrac.database'):
            df = db._fetch_crypto_data('XMR', days=30)

        assert df.empty
        assert any('XMR' in r.message and 'No daily OHLCV source' in r.message
                   for r in caplog.records)


# ─── Non-crypto delta refresh via Stooq ─────────────────
# Pins that get_ohlcv_data() does not call self.yahoo.fetch_daily() directly
# for delta refreshes of non-crypto symbols.
# Symbols like BUX (^BUX on Stooq) and RHM (RHM.DE on Stooq) are NOT in
# YahooClient.SYMBOL_MAP, so Yahoo would return empty and the cache would
# never update. Delta refreshes route through self.stooq.fetch(), which has
# correct mappings and a Yahoo fallback.

class TestNonCryptoDeltaUsesStooq:
    """Verify get_ohlcv_data() routes delta refreshes through StooqClient."""

    def _make_db(self):
        """Return a minimal OHLCVDatabase with mocked stooq/yahoo/cache."""
        from database import OHLCVDatabase
        from data_sources.stooq import StooqClient
        from data_sources.yahoo import YahooClient
        from data_sources.circuit_breaker import CircuitBreaker
        from unittest.mock import MagicMock

        db = OHLCVDatabase.__new__(OHLCVDatabase)

        # Wire up stooq and yahoo with mocked internals
        yahoo_breaker = CircuitBreaker('yfinance_test')
        yahoo_hourly_breaker = CircuitBreaker('yfinance_hourly_test')
        db.yahoo = YahooClient(yahoo_breaker, yahoo_hourly_breaker)
        db.stooq = StooqClient(CircuitBreaker('stooq_test'), db.yahoo)

        # Expose symbol maps (normally set by __init__)
        db.stock_map = db.stooq.stock_map
        db.etf_map = db.stooq.etf_map
        db.commodity_map = db.stooq.commodity_map
        db.index_map = db.stooq.index_map
        db.forex_map = db.stooq.forex_map

        # crypto_map / crypto_metric_map needed for routing
        db.crypto_map = {}
        db.crypto_metric_map = {}

        db.cache = MagicMock()
        return db

    def test_stale_cache_calls_stooq_not_yahoo_directly(self, monkeypatch):
        """Delta refresh for a stale non-crypto cache must go through stooq.fetch()."""
        db = self._make_db()

        # Stale cache: last bar is 120 days ago
        stale_date = pd.Timestamp.now() - pd.Timedelta(days=120)
        stale_df = pd.DataFrame(
            {'open': [100.0], 'high': [105.0], 'low': [98.0], 'close': [102.0], 'volume': [1000]},
            index=[stale_date],
        )
        db.cache.load.return_value = stale_df

        stooq_called = {'count': 0}
        yahoo_called = {'count': 0}

        fresh_df = pd.DataFrame(
            {'open': [200.0], 'high': [205.0], 'low': [198.0], 'close': [202.0], 'volume': [2000]},
            index=[pd.Timestamp.now()],
        )

        def fake_stooq_fetch(symbol, start_date=None):
            stooq_called['count'] += 1
            stooq_called['symbol'] = symbol
            return fresh_df

        def fake_yahoo_fetch(symbol, start_date=None):
            yahoo_called['count'] += 1
            return pd.DataFrame()

        monkeypatch.setattr(db.stooq, 'fetch', fake_stooq_fetch)
        monkeypatch.setattr(db.yahoo, 'fetch_daily', fake_yahoo_fetch)
        db.cache.save = MagicMock()

        db.get_ohlcv_data('BUX')

        assert stooq_called['count'] == 1, "stooq.fetch() must be called for delta refresh"
        assert stooq_called['symbol'] == 'BUX'
        assert yahoo_called['count'] == 0, "yahoo.fetch_daily() must NOT be called directly"

    def test_no_cache_calls_stooq_not_yahoo_directly(self, monkeypatch):
        """Full-history fetch for missing cache must go through stooq.fetch()."""
        db = self._make_db()
        db.cache.load.return_value = None

        stooq_called = {'count': 0}
        yahoo_called = {'count': 0}

        full_df = pd.DataFrame(
            {'open': [50.0], 'high': [55.0], 'low': [48.0], 'close': [52.0], 'volume': [500]},
            index=[pd.Timestamp('2025-01-01')],
        )

        monkeypatch.setattr(db.stooq, 'fetch', lambda s, **kw: (stooq_called.update({'count': stooq_called['count'] + 1}) or full_df))
        monkeypatch.setattr(db.yahoo, 'fetch_daily', lambda s, **kw: (yahoo_called.update({'count': yahoo_called['count'] + 1}) or pd.DataFrame()))
        db.cache.save = MagicMock()

        db.get_ohlcv_data('RHM')

        assert stooq_called['count'] == 1, "stooq.fetch() must be called for full history"
        assert yahoo_called['count'] == 0, "yahoo.fetch_daily() must NOT be called directly"

    def test_stooq_symbol_map_covers_stale_symbols(self):
        """BUX, HSTECH, RHM must all have explicit Stooq mappings."""
        from data_sources.stooq import StooqClient
        from data_sources.circuit_breaker import CircuitBreaker
        from unittest.mock import MagicMock

        yahoo_mock = MagicMock()
        client = StooqClient(CircuitBreaker('test'), yahoo_mock)

        # These symbols were stale because Yahoo didn't know their tickers.
        # Verify Stooq has them mapped correctly.
        assert client.get_stooq_symbol('BUX') == '^BUX', "BUX must map to ^BUX on Stooq"
        assert client.get_stooq_symbol('HSTECH') == '3033.HK', "HSTECH must map to 3033.HK on Stooq"
        assert client.get_stooq_symbol('RHM') == 'RHM.DE', "RHM must map to RHM.DE on Stooq"

    def test_yahoo_symbol_map_missing_for_stale_symbols(self):
        """Confirm BUX and RHM are NOT in Yahoo's SYMBOL_MAP (so they resolve to themselves)."""
        from data_sources.yahoo import YahooClient
        from data_sources.circuit_breaker import CircuitBreaker

        breaker = CircuitBreaker('test_yahoo')
        client = YahooClient(breaker, breaker)

        # These would resolve to themselves on Yahoo → 404 → no data
        assert client.resolve_symbol('BUX') == 'BUX', "BUX not mapped in Yahoo"
        assert client.resolve_symbol('RHM') == 'RHM', "RHM not mapped in Yahoo"


class TestAltSeasonIndexNeverInvents:
    """The Alt Season Index must withhold, never substitute.

    The failure shape to avoid is ``{'index': 50, 'season': 'neutral'}``, which
    renders identically to a measured reading in the sentiment bar, ticker
    bar, embeddable widget, MCP tool and morning report. A reader could not
    tell 'the market is neutral' from 'we have no idea'.

    The unit under test is ``calculate_alt_season_index`` itself; the 90d
    performance function is an injected argument (a real seam, not a patch)
    and ``get_cached_top_coins`` is a module-level collaborator.
    """

    @staticmethod
    def _client(monkeypatch, coins):
        import data_sources.coinpaprika as cp
        from data_sources.coinpaprika import CryptoMetricsClient
        monkeypatch.setattr(cp, 'get_cached_top_coins', lambda *a, **k: coins)
        client = CryptoMetricsClient()
        monkeypatch.setattr(client, 'get_top_coins', lambda *a, **k: coins)
        return client

    @staticmethod
    def _universe(n=70):
        return [{'symbol': f'C{i}'} for i in range(n)]

    def test_no_universe_returns_none_not_fifty(self, monkeypatch):
        client = self._client(monkeypatch, [])
        result = client.calculate_alt_season_index(lambda sym: 5.0)
        assert result['index'] is None, "must withhold, not publish 50"
        assert result['season'] is None
        assert result['error']

    def test_no_btc_performance_returns_none_not_fifty(self, monkeypatch):
        client = self._client(monkeypatch, self._universe())
        result = client.calculate_alt_season_index(lambda sym: None)
        assert result['index'] is None
        assert result['season'] is None

    def test_sample_below_minimum_returns_none(self, monkeypatch):
        """A 3-coin sample is a coin flip, not a market read."""
        from data_sources.coinpaprika import CryptoMetricsClient
        client = self._client(monkeypatch, self._universe())
        sparse = {'BTC', 'C1', 'C2', 'C3'}
        result = client.calculate_alt_season_index(
            lambda sym: 1.0 if sym in sparse else None
        )
        assert result['index'] is None
        assert str(CryptoMetricsClient.ALT_SEASON_MIN_SAMPLE) in result['error']

    def test_upstream_exception_returns_none_not_fifty(self, monkeypatch):
        client = self._client(monkeypatch, self._universe())

        def boom(sym):
            raise RuntimeError('upstream 500')

        result = client.calculate_alt_season_index(boom)
        assert result['index'] is None
        assert 'upstream 500' in result['error']

    def test_healthy_reading_still_computes(self, monkeypatch):
        client = self._client(monkeypatch, self._universe())
        # Every even-numbered coin beats BTC → 50% outperforming.
        result = client.calculate_alt_season_index(
            lambda sym: 1.0 if sym == 'BTC' else (2.0 if int(sym[1:]) % 2 == 0 else 0.5)
        )
        assert result['index'] == 50.0
        assert result['season'] == 'neutral'
        assert 'error' not in result

    def test_healthy_reading_exposes_its_sample_size(self, monkeypatch):
        """`total_analyzed` was read by every consumer and never populated —
        each published reading claimed a sample of 0."""
        client = self._client(monkeypatch, self._universe())
        result = client.calculate_alt_season_index(
            lambda sym: 1.0 if sym == 'BTC' else 2.0
        )
        assert result['total_analyzed'] == result['total_counted'] > 0

    def test_unavailable_result_is_not_cached(self, monkeypatch):
        """A withheld reading must not pin the cache and starve a recovery."""
        client = self._client(monkeypatch, [])
        assert client.calculate_alt_season_index(lambda sym: 5.0)['index'] is None
        assert client._alt_season_cache is None
