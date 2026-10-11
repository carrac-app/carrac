"""
Tests for market breadth — unit math, label thresholds, endpoint shape, cache behaviour.
"""

from unittest.mock import MagicMock, patch

import pandas as pd
import numpy as np
import pytest

# ---------------------------------------------------------------------------
# Helpers: synthetic OHLCV DataFrames
# ---------------------------------------------------------------------------

def _make_close(values, start='2024-01-01'):
    idx = pd.date_range(start, periods=len(values), freq='D')
    return pd.Series(values, index=idx, dtype=float)


def _make_df(close_values, start='2024-01-01'):
    """Return a minimal daily OHLCV DataFrame from a list of close prices."""
    close = _make_close(close_values, start=start)
    df = pd.DataFrame({
        'open':   close * 0.99,
        'high':   close * 1.01,
        'low':    close * 0.98,
        'close':  close,
        'volume': [1_000_000] * len(close),
    }, index=close.index)
    return df


# ---------------------------------------------------------------------------
# Pure-math helpers
# ---------------------------------------------------------------------------

from carrac.market.breadth import (
    _compute_rsi14,
    _compute_sma50,
    _compute_sma200,
    _is_20d_high,
    _is_20d_low,
    _derive_label,
    WASHED_OUT_THRESHOLD,
    WEAK_THRESHOLD,
    FROTHY_THRESHOLD,
    STRONG_THRESHOLD,
)


class TestComputeRsi14:
    def test_insufficient_data_returns_none(self):
        close = _make_close([100] * 10)
        assert _compute_rsi14(close) is None

    def test_all_gains_returns_100(self):
        # Strictly rising prices: no losses → RS = ∞ → RSI = 100
        close = _make_close(list(range(100, 150)))
        rsi = _compute_rsi14(close)
        assert rsi is not None
        assert rsi > 95

    def test_all_losses_returns_near_0(self):
        # Strictly falling prices: avg_gain = 0 → RSI = 0
        close = _make_close(list(range(150, 100, -1)))
        rsi = _compute_rsi14(close)
        assert rsi is not None
        assert rsi < 5

    def test_alternating_series_near_50(self):
        # Alternating up/down → RSI should hover near 50
        values = [100 + (1 if i % 2 == 0 else -1) for i in range(60)]
        close = _make_close(values)
        rsi = _compute_rsi14(close)
        assert rsi is not None
        assert 30 < rsi < 70

    def test_oversold_zone(self):
        # Drop from 100 to 50 in 60 bars → RSI should be oversold
        values = list(np.linspace(100, 50, 60))
        close = _make_close(values)
        rsi = _compute_rsi14(close)
        assert rsi is not None
        assert rsi < 30

    def test_overbought_zone(self):
        # Rise from 50 to 100 in 60 bars → RSI should be overbought
        values = list(np.linspace(50, 100, 60))
        close = _make_close(values)
        rsi = _compute_rsi14(close)
        assert rsi is not None
        assert rsi > 70

    def test_result_is_between_0_and_100(self):
        rng = np.random.default_rng(42)
        values = np.cumsum(rng.normal(0, 1, 100)) + 100
        close = _make_close(values.tolist())
        rsi = _compute_rsi14(close)
        assert rsi is not None
        assert 0 <= rsi <= 100


class TestComputeSma50:
    def test_insufficient_data_returns_none(self):
        close = _make_close([100] * 40)
        assert _compute_sma50(close) is None

    def test_flat_series_equals_price(self):
        close = _make_close([100.0] * 60)
        sma = _compute_sma50(close)
        assert sma == pytest.approx(100.0, rel=1e-6)

    def test_rising_series_sma_below_price(self):
        # Linearly rising series — SMA lags price
        values = list(range(1, 101))  # 1..100
        close = _make_close(values)
        sma = _compute_sma50(close)
        last_price = float(close.iloc[-1])
        assert sma is not None
        assert sma < last_price

    def test_falling_series_sma_above_price(self):
        values = list(range(100, 0, -1))
        close = _make_close(values)
        sma = _compute_sma50(close)
        last_price = float(close.iloc[-1])
        assert sma is not None
        assert sma > last_price


class TestNew20DHighLow:
    def test_insufficient_data_returns_none(self):
        close = _make_close([100] * 10)
        assert _is_20d_high(close) is None
        assert _is_20d_low(close) is None

    def test_new_high_when_at_peak(self):
        # Last value is a 20D high
        values = [100] * 19 + [110]
        close = _make_close(values)
        assert _is_20d_high(close) is True
        assert _is_20d_low(close) is False

    def test_new_low_when_at_trough(self):
        # Last value is a 20D low
        values = [100] * 19 + [90]
        close = _make_close(values)
        assert _is_20d_low(close) is True
        assert _is_20d_high(close) is False

    def test_middle_value_is_neither(self):
        # Last value is neither high nor low
        values = list(range(80, 100)) + [90]  # 20 bars, last is in the middle
        close = _make_close(values)
        assert _is_20d_high(close) is False
        assert _is_20d_low(close) is False

    def test_flat_series_counts_as_high_and_low(self):
        # When all values equal, last == rolling_high == rolling_low
        close = _make_close([100.0] * 25)
        assert _is_20d_high(close) is True
        assert _is_20d_low(close) is True


# ---------------------------------------------------------------------------
# Label thresholds
# ---------------------------------------------------------------------------

class TestDeriveLabel:
    """Verify all five label cases and boundary conditions."""

    def test_washed_out(self):
        assert _derive_label(WASHED_OUT_THRESHOLD, 0) == 'washed-out'
        assert _derive_label(WASHED_OUT_THRESHOLD + 10, 0) == 'washed-out'

    def test_washed_out_beats_frothy(self):
        # Washed-out condition checked before frothy — washed-out wins
        assert _derive_label(WASHED_OUT_THRESHOLD, FROTHY_THRESHOLD + 5) == 'washed-out'

    def test_frothy(self):
        assert _derive_label(0, FROTHY_THRESHOLD) == 'frothy'
        assert _derive_label(5, FROTHY_THRESHOLD + 5) == 'frothy'

    def test_weak(self):
        assert _derive_label(WEAK_THRESHOLD, 0) == 'weak'
        assert _derive_label(WEAK_THRESHOLD + 5, 0) == 'weak'
        # Just below WASHED_OUT
        assert _derive_label(WASHED_OUT_THRESHOLD - 0.1, 0) == 'weak'

    def test_strong(self):
        assert _derive_label(0, STRONG_THRESHOLD) == 'strong'
        assert _derive_label(WEAK_THRESHOLD - 0.1, STRONG_THRESHOLD) == 'strong'

    def test_mixed_default(self):
        assert _derive_label(5, 5) == 'mixed'
        assert _derive_label(0, 0) == 'mixed'
        # oversold just below WEAK but OB below STRONG → mixed
        assert _derive_label(WEAK_THRESHOLD - 0.5, STRONG_THRESHOLD - 0.5) == 'mixed'

    def test_strong_requires_low_oversold(self):
        # If oversold is at WEAK threshold, strong should not fire
        assert _derive_label(WEAK_THRESHOLD, STRONG_THRESHOLD) != 'strong'


# ---------------------------------------------------------------------------
# compute_breadth: integration over a synthetic CSV cache
# ---------------------------------------------------------------------------

class TestComputeBreadth:
    """Test compute_breadth() with a patched CSVCache."""

    def _mock_csv_cache(self, symbol_data: dict):
        """
        Returns a CSVCache mock whose .load() returns DataFrames from symbol_data.
        symbol_data: {symbol: list_of_close_values}
        """
        mock = MagicMock()
        def load_side_effect(symbol, timeframe='daily'):
            values = symbol_data.get(symbol)
            if values is None:
                return None
            return _make_df(values)
        mock.load.side_effect = load_side_effect
        return mock

    def _run(self, symbol_data, universe=None):
        from carrac.market import breadth as breadth_mod
        if universe is None:
            universe = list(symbol_data.keys())

        mock_csv = self._mock_csv_cache(symbol_data)

        with (
            patch('carrac.market.breadth._build_universe', return_value=universe),
            # CSVCache is imported inside compute_breadth as `from csv_cache import CSVCache`
            # so we patch it in the csv_cache module namespace.
            patch('csv_cache.CSVCache', return_value=mock_csv),
        ):
            return breadth_mod.compute_breadth()

    def test_empty_universe(self):
        result = self._run({}, universe=[])
        assert result['universe_size'] == 0
        assert result['covered'] == 0
        assert result['label'] == 'mixed'

    def test_all_oversold(self):
        # 60 strictly falling bars → all oversold
        falling = list(np.linspace(200, 50, 60))
        data = {f'SYM{i}': falling for i in range(5)}
        result = self._run(data)
        assert result['covered'] == 5
        assert result['oversold']['count'] == 5
        assert result['oversold']['pct'] == 100.0
        assert result['label'] == 'washed-out'

    def test_all_overbought(self):
        rising = list(np.linspace(50, 200, 60))
        data = {f'SYM{i}': rising for i in range(5)}
        result = self._run(data)
        assert result['covered'] == 5
        assert result['overbought']['count'] == 5
        assert result['overbought']['pct'] == 100.0
        assert result['label'] == 'frothy'

    def test_above_sma50_correct(self):
        # Linearly rising 100 bars: last price > SMA50
        rising = list(range(1, 101))
        data = {'AAPL': rising}
        result = self._run(data)
        assert result['above_sma50']['count'] == 1
        assert result['below_sma50']['count'] == 0

    def test_below_sma50_correct(self):
        # Linearly falling 100 bars: last price < SMA50
        falling = list(range(100, 0, -1))
        data = {'MSFT': falling}
        result = self._run(data)
        assert result['below_sma50']['count'] == 1
        assert result['above_sma50']['count'] == 0

    def test_new_20d_high(self):
        # Last value is a 25-bar high
        values = [100] * 24 + [110]
        data = {'BTC': values}
        result = self._run(data)
        assert result['new_20d_highs']['count'] == 1
        assert result['new_20d_lows']['count'] == 0

    def test_new_20d_low(self):
        values = [100] * 24 + [90]
        data = {'ETH': values}
        result = self._run(data)
        assert result['new_20d_lows']['count'] == 1
        assert result['new_20d_highs']['count'] == 0

    def test_missing_symbol_skipped(self):
        # Universe includes 'MISSING' which has no CSV
        rising = list(range(1, 101))
        data = {'PRESENT': rising}
        result = self._run(data, universe=['PRESENT', 'MISSING'])
        assert result['universe_size'] == 2
        assert result['covered'] == 1  # MISSING skipped

    def test_symbols_sample_capped_at_5(self):
        # 10 oversold symbols — sample must be ≤ 5
        falling = list(np.linspace(200, 50, 60))
        data = {f'SYM{i}': falling for i in range(10)}
        result = self._run(data)
        assert len(result['oversold']['symbols_sample']) <= 5

    def test_result_keys_complete(self):
        rising = list(range(1, 101))
        result = self._run({'BTC': rising})
        required_keys = {
            'as_of', 'universe_size', 'covered',
            'oversold', 'overbought',
            'above_sma50', 'below_sma50',
            'new_20d_highs', 'new_20d_lows',
            'label', '_computed_in_s',
        }
        assert required_keys.issubset(result.keys())

    def test_pct_sums_near_100_for_sma50(self):
        # For symbols with enough bars (50+), above + below should = 100%
        rising = list(range(1, 101))
        falling = list(range(100, 0, -1))
        data = {'UP': rising, 'DOWN': falling}
        result = self._run(data)
        total = result['above_sma50']['pct'] + result['below_sma50']['pct']
        assert total == pytest.approx(100.0, abs=1.0)


# ---------------------------------------------------------------------------
# Endpoint shape + cache behaviour (Flask test client)
# ---------------------------------------------------------------------------

class TestBreadthEndpoint:
    """Test /api/market/breadth via the Flask test client."""

    def _stub_breadth(self):
        return {
            'as_of': '2026-01-01T00:00:00Z',
            'universe_size': 5,
            'covered': 4,
            'oversold': {'count': 1, 'pct': 25.0, 'symbols_sample': ['BTC']},
            'overbought': {'count': 1, 'pct': 25.0, 'symbols_sample': ['ETH']},
            'above_sma50': {'count': 2, 'pct': 50.0},
            'below_sma50': {'count': 2, 'pct': 50.0},
            'new_20d_highs': {'count': 0, 'pct': 0.0, 'symbols_sample': []},
            'new_20d_lows': {'count': 0, 'pct': 0.0, 'symbols_sample': []},
            'label': 'mixed',
            '_computed_in_s': 0.01,
        }

    def test_requires_auth(self, client):
        # Unauthenticated: auth_or_key_required redirects to login (302) or 401
        resp = client.get('/api/market/breadth')
        assert resp.status_code in (302, 401, 403)

    def test_authenticated_returns_200(self, auth_client, monkeypatch):
        monkeypatch.setattr('carrac.market.routes.get_breadth',
                            lambda **kw: self._stub_breadth())
        resp = auth_client.get('/api/market/breadth')
        assert resp.status_code == 200

    def test_response_has_required_fields(self, auth_client, monkeypatch):
        monkeypatch.setattr('carrac.market.routes.get_breadth',
                            lambda **kw: self._stub_breadth())
        resp = auth_client.get('/api/market/breadth')
        data = resp.get_json()
        for key in ('as_of', 'universe_size', 'covered', 'oversold',
                    'overbought', 'above_sma50', 'below_sma50',
                    'new_20d_highs', 'new_20d_lows', 'label'):
            assert key in data, f'missing key: {key}'

    def test_oversold_has_symbols_sample(self, auth_client, monkeypatch):
        monkeypatch.setattr('carrac.market.routes.get_breadth',
                            lambda **kw: self._stub_breadth())
        resp = auth_client.get('/api/market/breadth')
        data = resp.get_json()
        assert 'symbols_sample' in data['oversold']
        assert isinstance(data['oversold']['symbols_sample'], list)

    def test_label_is_string(self, auth_client, monkeypatch):
        monkeypatch.setattr('carrac.market.routes.get_breadth',
                            lambda **kw: self._stub_breadth())
        resp = auth_client.get('/api/market/breadth')
        data = resp.get_json()
        assert data['label'] in ('washed-out', 'weak', 'mixed', 'strong', 'frothy')

    def test_cache_hit_on_second_call(self, auth_client, monkeypatch):
        """Second call must return cached data without re-invoking compute_breadth."""
        import carrac.market.breadth as breadth_mod

        call_count = {'n': 0}
        stub = self._stub_breadth()


        def counting_compute():
            call_count['n'] += 1
            return stub

        # Seed cache
        with patch.object(breadth_mod, 'compute_breadth', counting_compute):
            breadth_mod.get_breadth(force_refresh=True)

        # Second call — should read from in-memory cache (compute_breadth not called)
        call_count['n'] = 0
        monkeypatch.setattr('carrac.market.routes.get_breadth',
                            lambda **kw: breadth_mod.get_breadth())

        with (
            patch.object(breadth_mod, 'compute_breadth', counting_compute),
            patch.object(breadth_mod, '_get_redis', return_value=None),
        ):
            auth_client.get('/api/market/breadth')
            assert call_count['n'] == 0, 'compute_breadth should not be called on cache hit'

    def test_503_on_exception(self, auth_client, monkeypatch):
        def _raise(**kw):
            raise RuntimeError('simulated failure')
        monkeypatch.setattr('carrac.market.routes.get_breadth', _raise)
        resp = auth_client.get('/api/market/breadth')
        assert resp.status_code == 503

    def test_api_key_auth_allowed(self, client, monkeypatch):
        """auth_or_key_required should accept API-key requests."""
        import carrac.auth.decorators as _auth
        # Patch verify_api_key to always succeed
        monkeypatch.setattr(_auth, 'verify_api_key', lambda k: k == 'good-key')
        monkeypatch.setattr('carrac.market.routes.get_breadth',
                            lambda **kw: self._stub_breadth())
        resp = client.get('/api/market/breadth',
                          headers={'X-API-Key': 'good-key'})
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Unit tests for _compute_sma200 helper
# ---------------------------------------------------------------------------

class TestComputeSma200:
    def test_insufficient_data_returns_none(self):
        close = _make_close([100] * 150)
        assert _compute_sma200(close) is None

    def test_exactly_199_bars_returns_none(self):
        close = _make_close([100.0] * 199)
        assert _compute_sma200(close) is None

    def test_exactly_200_bars_returns_value(self):
        close = _make_close([100.0] * 200)
        sma = _compute_sma200(close)
        assert sma is not None
        assert sma == pytest.approx(100.0, rel=1e-6)

    def test_flat_series_equals_price(self):
        close = _make_close([50.0] * 250)
        sma = _compute_sma200(close)
        assert sma == pytest.approx(50.0, rel=1e-6)

    def test_rising_series_sma_below_price(self):
        # Linearly rising: SMA(200) lags price
        values = list(range(1, 251))  # 1..250, last = 250
        close = _make_close(values)
        sma = _compute_sma200(close)
        last_price = float(close.iloc[-1])
        assert sma is not None
        assert sma < last_price

    def test_falling_series_sma_above_price(self):
        values = list(range(250, 0, -1))  # 250..1, last = 1
        close = _make_close(values)
        sma = _compute_sma200(close)
        last_price = float(close.iloc[-1])
        assert sma is not None
        assert sma > last_price


# ---------------------------------------------------------------------------
# compute_breadth: new fields — above_sma200 / advancers / decliners
# ---------------------------------------------------------------------------

class TestComputeBreadthNewFields:
    """
    Fixture universe (3 symbols):
      RISING_250:  250 bars, list(range(1, 251))    — rising  → above SMA200, advancer
      FALLING_250: 250 bars, list(range(250, 0,-1)) — falling → below SMA200, decliner
      FLAT_25:      25 bars, [100.0]*25             — too short for SMA200, flat → neither

    Expected (n=3 covered, all have ≥ 20 bars):
      above_sma200 count = 1  (RISING_250 only; FALLING_250 below; FLAT_25 excluded)
      above_sma200 pct   = round(1/3*100, 1) = 33.3
      advancers = 1  (RISING_250: close[-1]=250 > close[-2]=249)
      decliners = 1  (FALLING_250: close[-1]=1  < close[-2]=2)
    """

    _RISING_250 = list(range(1, 251))
    _FALLING_250 = list(range(250, 0, -1))
    _FLAT_25 = [100.0] * 25

    def _mock_csv_cache(self, symbol_data: dict):
        mock = MagicMock()
        def load_side_effect(symbol, timeframe='daily'):
            values = symbol_data.get(symbol)
            if values is None:
                return None
            return _make_df(values)
        mock.load.side_effect = load_side_effect
        return mock

    def _run(self, symbol_data, universe=None):
        from carrac.market import breadth as breadth_mod
        if universe is None:
            universe = list(symbol_data.keys())
        mock_csv = self._mock_csv_cache(symbol_data)
        with (
            patch('carrac.market.breadth._build_universe', return_value=universe),
            patch('csv_cache.CSVCache', return_value=mock_csv),
        ):
            return breadth_mod.compute_breadth()

    @property
    def _universe(self):
        return {
            'RISING_250':  self._RISING_250,
            'FALLING_250': self._FALLING_250,
            'FLAT_25':     self._FLAT_25,
        }

    def test_above_sma200_count(self):
        result = self._run(self._universe)
        assert result['above_sma200']['count'] == 1

    def test_above_sma200_pct(self):
        # 1 of 3 covered → round(100/3, 1) = 33.3
        result = self._run(self._universe)
        assert result['above_sma200']['pct'] == pytest.approx(33.3, abs=0.1)

    def test_above_sma200_rising_only(self):
        # Only the rising symbol should be above SMA200; verify with single-symbol runs
        result_up = self._run({'RISING_250': self._RISING_250})
        assert result_up['above_sma200']['count'] == 1

        result_down = self._run({'FALLING_250': self._FALLING_250})
        assert result_down['above_sma200']['count'] == 0

    def test_above_sma200_excluded_when_too_short(self):
        # Symbol with < 200 bars contributes to covered but not to above_sma200
        result = self._run({'FLAT_25': self._FLAT_25})
        assert result['above_sma200']['count'] == 0

    def test_advancers_count(self):
        result = self._run(self._universe)
        # RISING_250: last=250 > prev=249 → advancer
        assert result['advancers'] == 1

    def test_decliners_count(self):
        result = self._run(self._universe)
        # FALLING_250: last=1 < prev=2 → decliner
        assert result['decliners'] == 1

    def test_flat_symbol_is_neither_advancer_nor_decliner(self):
        # Flat series: last == prev → neither
        result = self._run({'FLAT_25': self._FLAT_25})
        assert result['advancers'] == 0
        assert result['decliners'] == 0

    def test_advancers_decliners_sum_le_covered(self):
        result = self._run(self._universe)
        assert result['advancers'] + result['decliners'] <= result['covered']

    def test_new_fields_present_in_result(self):
        result = self._run(self._universe)
        assert 'above_sma200' in result
        assert 'advancers' in result
        assert 'decliners' in result

    def test_above_sma200_has_count_and_pct(self):
        result = self._run(self._universe)
        assert 'count' in result['above_sma200']
        assert 'pct' in result['above_sma200']
