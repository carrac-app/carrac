"""
Unit tests for Database Module
Tests TTLCache, data fetching, and caching logic
"""

import unittest
import sys
import os
from datetime import datetime, timedelta

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
import pandas as pd

class TestGetCombinedMarketCapsCacheFirst:
    """_get_combined_market_caps must prefer the warm per-symbol
    CSV cache (the one the ~350-symbol precompute pass keeps warm) over a
    live Binance fetch, only falling back to a live fetch — bounded to a
    small thread pool — for symbols with no cache entry at all.
    """

    def _make_db(self, monkeypatch, top_coins, csv_cache_data):
        """Build a bare OHLCVDatabase with just enough wired to reach Tier 3
        of _get_combined_market_caps (both outer caches report cold)."""
        import database as database_module
        from database import OHLCVDatabase

        db = OHLCVDatabase.__new__(OHLCVDatabase)

        # Force both outer cache tiers to report a miss so the test exercises
        # the constituent-fetch loop under test.
        monkeypatch.setattr(database_module, 'get_cached_market_caps', lambda: None)
        monkeypatch.setattr(database_module, 'set_cached_market_caps', lambda data: None)

        class _FakeMarketCapFileCache:
            def load(self):
                return None

            def save(self, df, coins):
                pass

        db.market_cap_cache = _FakeMarketCapFileCache()
        db._get_top_coins = lambda num_coins: top_coins

        class _FakeCSVCache:
            def load(self, symbol, timeframe='daily'):
                return csv_cache_data.get(symbol)

        db.cache = _FakeCSVCache()
        return db

    @staticmethod
    def _price_df(price, days=3):
        idx = pd.date_range(end=datetime.now().date(), periods=days, freq='D')
        return pd.DataFrame({
            'open': price, 'high': price, 'low': price, 'close': price, 'volume': 100.0,
        }, index=idx)

    def test_warm_cache_hit_never_touches_live_fetch(self, monkeypatch):
        """Symbols with a warm CSV cache entry must be served from disk —
        _fetch_crypto_data (the live Binance/HL/CCXT/CC chain) must not be
        called at all when every constituent is already cached."""
        top_coins = [
            {'symbol': 'BTC', 'market_cap': 2_000_000_000_000, 'price': 100_000.0},
            {'symbol': 'ETH', 'market_cap': 400_000_000_000, 'price': 4_000.0},
        ]
        csv_cache_data = {
            'BTC': self._price_df(100_000.0),
            'ETH': self._price_df(4_000.0),
        }
        db = self._make_db(monkeypatch, top_coins, csv_cache_data)

        live_calls = []

        def _fake_fetch(symbol, days):
            live_calls.append(symbol)
            return pd.DataFrame()

        db._fetch_crypto_data = _fake_fetch

        combined = db._get_combined_market_caps(days=3, num_coins=2)

        assert live_calls == [], "cache-hit constituents must never hit the live fetch path"
        assert set(combined.columns) == {'BTC', 'ETH'}
        expected_btc = 100_000.0 * (2_000_000_000_000 / 100_000.0)
        assert combined['BTC'].iloc[-1] == pytest.approx(expected_btc)

    def test_uncached_symbol_falls_back_to_live_fetch(self, monkeypatch):
        """A symbol with NO cache entry (never precomputed) must still be
        live-fetched — the fallback path is preserved exactly."""
        top_coins = [
            {'symbol': 'BTC', 'market_cap': 2_000_000_000_000, 'price': 100_000.0},
            {'symbol': 'OBSCURE', 'market_cap': 1_000_000.0, 'price': 1.0},
        ]
        csv_cache_data = {'BTC': self._price_df(100_000.0)}
        db = self._make_db(monkeypatch, top_coins, csv_cache_data)

        live_calls = []

        def _fake_fetch(symbol, days):
            live_calls.append(symbol)
            return self._price_df(1.0)

        db._fetch_crypto_data = _fake_fetch

        combined = db._get_combined_market_caps(days=3, num_coins=2)

        assert live_calls == ['OBSCURE']
        assert set(combined.columns) == {'BTC', 'OBSCURE'}

    def test_stablecoins_skip_cache_and_live_fetch(self, monkeypatch):
        """USDT/USDC use the synthetic constant-1.0 supply path — they must
        never touch the CSV cache or the live fetch path."""
        top_coins = [
            {'symbol': 'USDT', 'market_cap': 100_000_000_000.0, 'price': 1.0},
        ]
        db = self._make_db(monkeypatch, top_coins, csv_cache_data={})

        cache_calls = []
        db.cache.load = lambda symbol, timeframe='daily': cache_calls.append(symbol)
        db._fetch_crypto_data = lambda symbol, days: pytest.fail(
            "stablecoins must not hit the live fetch path"
        )

        combined = db._get_combined_market_caps(days=3, num_coins=1)

        assert cache_calls == []
        assert 'USDT' in combined.columns

    def test_dominance_math_unchanged_from_cache_sourced_constituents(self, monkeypatch):
        """End-to-end: _fetch_crypto_metric_data('BTC.D', ...) computed from
        cache-sourced constituents must match hand-computed dominance — i.e.
        the data-sourcing change left the downstream math (which
        this change does not touch) byte-for-byte identical."""
        top_coins = [
            {'symbol': 'BTC', 'market_cap': 2_000_000_000_000.0, 'price': 100_000.0},
            {'symbol': 'ETH', 'market_cap': 500_000_000_000.0, 'price': 5_000.0},
            {'symbol': 'USDT', 'market_cap': 500_000_000_000.0, 'price': 1.0},
        ]
        csv_cache_data = {
            'BTC': self._price_df(100_000.0, days=5),
            'ETH': self._price_df(5_000.0, days=5),
        }
        db = self._make_db(monkeypatch, top_coins, csv_cache_data)
        db._fetch_crypto_data = lambda symbol, days: pytest.fail(
            "all constituents are cached — must not live-fetch"
        )
        db.crypto_metric_map = {'BTC.D': 'btc_dominance'}

        df = db._fetch_crypto_metric_data('BTC.D', days=5)

        total_mcap = 2_000_000_000_000.0 + 500_000_000_000.0 + 500_000_000_000.0
        expected_dominance = 2_000_000_000_000.0 / total_mcap * 100
        assert df['close'].iloc[-1] == pytest.approx(expected_dominance)

    def test_mixed_coverage_short_constituent_does_not_distort_prev_month(self, monkeypatch):
        """Folding a short- / misaligned-coverage constituent in
        with a full-depth one must NOT distort the summed series over the
        previous full month (the window CPR reads prevHigh/prevLow/prevClose
        from). Pins: a constituent whose cache only holds a few bars —
        stamped off the deep daily grid — must not produce phantom previous-month
        rows where the deep constituents are absent, which collapse the summed
        low toward ~0."""
        deep_days = 120
        deep_idx = pd.date_range(end=datetime.now().date(), periods=deep_days, freq='D')
        btc_df = pd.DataFrame({
            'open': 100_000.0, 'high': 100_000.0, 'low': 100_000.0,
            'close': 100_000.0, 'volume': 100.0,
        }, index=deep_idx)

        # Short constituent: a handful of previous-month bars stamped at noon
        # so a naive union scatters them onto phantom dates the deep
        # constituent never covers. Tiny market cap so any leakage collapses
        # the summed low toward ~0.
        today = pd.Timestamp(datetime.now().date())
        prev_month = (today.replace(day=1) - pd.Timedelta(days=1)).replace(day=1)
        short_idx = pd.date_range(
            start=prev_month + pd.Timedelta(days=9, hours=12), periods=5, freq='D'
        )
        short_df = pd.DataFrame({
            'open': 0.01, 'high': 0.01, 'low': 0.01, 'close': 0.01, 'volume': 5.0,
        }, index=short_idx)

        top_coins = [
            {'symbol': 'BTC', 'market_cap': 2_000_000_000_000.0, 'price': 100_000.0},
            {'symbol': 'SHORT', 'market_cap': 1_000_000.0, 'price': 0.01},
        ]
        csv_cache_data = {'BTC': btc_df, 'SHORT': short_df}
        db = self._make_db(monkeypatch, top_coins, csv_cache_data)
        db._fetch_crypto_data = lambda symbol, days: pytest.fail(
            "all constituents are cached — must not live-fetch"
        )

        combined = db._get_combined_market_caps(days=deep_days, num_coins=2)

        # The coverage gate must drop the under-covered constituent entirely.
        assert 'SHORT' not in combined.columns
        assert 'BTC' in combined.columns

        # The summed series over the previous full month must stay at the deep
        # constituent's level (~2e12), not collapse toward the tiny SHORT cap.
        total = combined.sum(axis=1)
        prev = total[(total.index.year == prev_month.year)
                     & (total.index.month == prev_month.month)]
        assert not prev.empty
        expected = 100_000.0 * (2_000_000_000_000.0 / 100_000.0)  # 2e12
        assert prev.min() == pytest.approx(expected, rel=1e-6)

    def test_end_truncated_zombie_constituent_dropped_from_recent_window(self, monkeypatch):
        """A constituent with DEEP start-history whose
        coverage STOPS well before ref_end (a delisted/zombie pair — FTM/RNDR
        class) passes a start-depth test yet its absence from recent days
        punches a step-dip into the summed series that CPR reads from. Seen
        on real data: TOTAL constituents M/GT/LEO/GRAM held only
        67–90% of the June/July window, swinging the sum 1.9T↔6.3T. The gate
        must drop such constituents on recent-window density/freshness, not
        only on start-depth."""
        today = pd.Timestamp(datetime.now().date())
        deep_days = 120

        btc_idx = pd.date_range(end=today, periods=deep_days, freq='D')
        btc_df = pd.DataFrame({
            'open': 100_000.0, 'high': 100_000.0, 'low': 100_000.0,
            'close': 100_000.0, 'volume': 100.0,
        }, index=btc_idx)

        # Deep START (120 bars, reaching back well before last month) but its
        # coverage ENDS 12 days before today — the zombie shape.
        zombie_end = today - pd.Timedelta(days=12)
        zombie_idx = pd.date_range(end=zombie_end, periods=deep_days, freq='D')
        zombie_df = pd.DataFrame({
            'open': 3.0, 'high': 3.0, 'low': 3.0, 'close': 3.0, 'volume': 50.0,
        }, index=zombie_idx)

        top_coins = [
            {'symbol': 'BTC', 'market_cap': 2_000_000_000_000.0, 'price': 100_000.0},
            {'symbol': 'ZOMBIE', 'market_cap': 3_000_000_000_000.0, 'price': 3.0},
        ]
        csv_cache_data = {'BTC': btc_df, 'ZOMBIE': zombie_df}
        db = self._make_db(monkeypatch, top_coins, csv_cache_data)
        db._fetch_crypto_data = lambda symbol, days: pytest.fail(
            "all constituents are cached — must not live-fetch"
        )

        combined = db._get_combined_market_caps(days=deep_days, num_coins=2)

        # The gate must drop the end-truncated zombie despite its deep start.
        assert 'ZOMBIE' not in combined.columns
        assert 'BTC' in combined.columns

        # With the zombie gone there is no step-dip — the summed series is the
        # deep constituent's flat level across the whole retained window.
        total = combined.sum(axis=1)
        expected = 100_000.0 * (2_000_000_000_000.0 / 100_000.0)  # 2e12
        assert total.min() == pytest.approx(expected, rel=1e-6)
        assert total.max() == pytest.approx(expected, rel=1e-6)

    def test_redenominated_constituent_dropped_on_value_sanity(self, monkeypatch):
        """A DENSE, FRESH constituent whose historical
        closes are on a different scale than its live price (a rebrand /
        redenomination — SKY post-Maker) produces an absurd scaled cap that
        passes the coverage gate yet swings the summed series. Seen on real
        data: SKY's scaled cap spiked to ~1.95T, ~1400x its ~1.5B
        snapshot. The value-sanity check must drop it on the snapshot-cap
        mismatch."""
        today = pd.Timestamp(datetime.now().date())
        days = 120
        idx = pd.date_range(end=today, periods=days, freq='D')

        btc_df = pd.DataFrame({
            'open': 100_000.0, 'high': 100_000.0, 'low': 100_000.0,
            'close': 100_000.0, 'volume': 100.0,
        }, index=idx)

        # Full coverage (dense + fresh) but historical closes spike ~300x its
        # live price on scattered days — an impossible price move, so the
        # implied cap dwarfs the snapshot cap.
        closes = pd.Series(1.0, index=idx)
        closes.iloc[-30:-25] = 300.0  # recent-window spike
        spike_df = pd.DataFrame({
            'open': closes, 'high': closes, 'low': closes,
            'close': closes, 'volume': 10.0,
        }, index=idx)

        top_coins = [
            {'symbol': 'BTC', 'market_cap': 2_000_000_000_000.0, 'price': 100_000.0},
            # snapshot cap 1B at price 1.0 → supply 1e9; a 300.0 close implies
            # a 300B cap = 300x the snapshot.
            {'symbol': 'SPIKE', 'market_cap': 1_000_000_000.0, 'price': 1.0},
        ]
        csv_cache_data = {'BTC': btc_df, 'SPIKE': spike_df}
        db = self._make_db(monkeypatch, top_coins, csv_cache_data)
        db._fetch_crypto_data = lambda symbol, days: pytest.fail(
            "all constituents are cached — must not live-fetch"
        )

        combined = db._get_combined_market_caps(days=days, num_coins=2)

        # The value-sanity check must drop the rescaled constituent even though
        # its coverage is complete.
        assert 'SPIKE' not in combined.columns
        assert 'BTC' in combined.columns

        # No spike leaks into the summed series — it is BTC's flat level.
        total = combined.sum(axis=1)
        expected = 100_000.0 * (2_000_000_000_000.0 / 100_000.0)  # 2e12
        assert total.min() == pytest.approx(expected, rel=1e-6)
        assert total.max() == pytest.approx(expected, rel=1e-6)


class TestSyntheticCandleSanityGate:
    """A frozen/delisted Binance ticker (dead order book, stale lastPrice)
    can still slip through and get spliced onto stale history by
    inject_synthetic_candle, corrupting CPR/RSI/BB. get_multi_timeframe_data_
    with_realtime must reject an implausible realtime price instead of
    injecting it blindly."""

    def _make_db(self):
        from database import OHLCVDatabase
        db = OHLCVDatabase.__new__(OHLCVDatabase)
        db.price_injection_rejects = set()
        return db

    @staticmethod
    def _history_df(closes):
        idx = pd.date_range(end=datetime.now().date() - timedelta(days=1), periods=len(closes), freq='D')
        return pd.DataFrame({
            'open': closes, 'high': closes, 'low': closes, 'close': closes, 'volume': 100.0,
        }, index=idx)

    def test_4x_price_rejected(self, monkeypatch):
        """RNDR-style case: history sits ~1.68, a frozen ticker reports 7.03
        (>50% deviation) — must be rejected, df left unchanged, symbol
        recorded in price_injection_rejects.

        SAND stands in for RNDR: RNDR is now on
        instrument_master.LIFECYCLE_OVERRIDES, so it would be rejected by the
        delisting-gate branch BEFORE the deviation is ever measured — this
        test would still pass while testing nothing.
        """
        db = self._make_db()
        history = self._history_df([1.65, 1.66, 1.68, 1.70, 1.69])
        monkeypatch.setattr(db, 'get_ohlcv_data', lambda symbol: history)

        result = db.get_multi_timeframe_data_with_realtime('SAND', realtime_price=7.03)

        assert list(result['1D']['close']) == list(history['close'])
        assert 'SAND' in db.price_injection_rejects

    def test_20pct_move_injected_normally(self, monkeypatch):
        """A plausible +20% move must still inject the synthetic candle and
        must not be flagged as rejected."""
        db = self._make_db()
        history = self._history_df([100.0, 101.0, 99.0, 102.0, 100.0])
        monkeypatch.setattr(db, 'get_ohlcv_data', lambda symbol: history)

        result = db.get_multi_timeframe_data_with_realtime('BTC', realtime_price=120.0)

        assert result['1D']['close'].iloc[-1] == pytest.approx(120.0)
        assert 'BTC' not in db.price_injection_rejects

    def test_recovery_clears_previous_reject(self, monkeypatch):
        """A symbol that was rejected must be cleared from the set once a
        sane price is seen again. SAND, not RNDR — see test_4x_price_rejected."""
        db = self._make_db()
        db.price_injection_rejects.add('SAND')
        history = self._history_df([1.65, 1.66, 1.68, 1.70, 1.69])
        monkeypatch.setattr(db, 'get_ohlcv_data', lambda symbol: history)

        db.get_multi_timeframe_data_with_realtime('SAND', realtime_price=1.72)

        assert 'SAND' not in db.price_injection_rejects

    def test_a_delisted_symbol_never_gets_a_live_price_spliced_on(self, monkeypatch):
        """At the gate that matters most.

        This guard asks `_is_history_stale`, and a delisted instrument is stale
        by definition however recent its last bar happens to be. Without that,
        a symbol whose collection stopped four days ago would have a live tick
        injected onto history that has no future — which is the earlier
        fabrication this gate exists to prevent, arriving through the one door
        the bar-age check leaves open.
        """
        import instrument_master as im

        assert im.is_delisted('RNDR')
        db = self._make_db()
        history = self._history_df([1.65, 1.66, 1.68, 1.70, 1.69])
        monkeypatch.setattr(db, 'get_ohlcv_data', lambda symbol: history)

        result = db.get_multi_timeframe_data_with_realtime('RNDR', realtime_price=1.72)

        assert list(result['1D']['close']) == list(history['close']), (
            'a live price was spliced onto a delisted series'
        )
        assert 'RNDR' in db.price_injection_rejects


class TestNativeCrossPairOHLC:
    """ETHBTC/XRPBTC/AVAXBTC are real Binance spot markets, but were built as
    a synthetic close/close ratio — open==high==low==close on every bar, so
    klinecharts drew a flat horizontal line per bar instead of a candle.
    These pairs must be served from their native market, which has real OHLC
    and real volume."""

    def _make_db(self):
        from database import OHLCVDatabase
        db = OHLCVDatabase.__new__(OHLCVDatabase)
        db.crypto_metric_map = {
            'ETHBTC': 'ethbtc_ratio',
            'XRPBTC': 'xrpbtc_ratio',
            'AVAXBTC': 'avaxbtc_ratio',
        }
        return db

    @staticmethod
    def _native_df(days=5):
        idx = pd.date_range(end=datetime.now().date(), periods=days, freq='D')
        return pd.DataFrame({
            'open': 0.0296, 'high': 0.0299, 'low': 0.0292,
            'close': 0.0294, 'volume': 2586.4,
        }, index=idx)

    def test_native_pair_ohlc_is_not_flattened(self):
        """The native market's distinct open/high/low/close must survive to the
        chart — a bar where they are all equal renders as a flat line."""
        db = self._make_db()
        db.binance = type('B', (), {
            'fetch_pair_ohlcv': staticmethod(lambda pair, days: self._native_df())
        })()
        db._fetch_crypto_data = lambda symbol, days: pytest.fail(
            "native pair must not fall back to the synthetic ratio path"
        )

        df = db._fetch_crypto_metric_data('ETHBTC', days=5)

        assert not df.empty
        assert (df['high'] > df['low']).all(), "high must exceed low on a real candle"
        assert (df['open'] != df['close']).all()
        assert (df['volume'] > 0).all(), "native market has real volume"

    def test_native_pair_requested_by_exact_symbol(self):
        """The Binance pair is the metric symbol itself (ETHBTC), not the
        USDT-quoted pair the standard crypto path builds."""
        db = self._make_db()
        seen = []

        def _fetch_pair(pair, days):
            seen.append(pair)
            return self._native_df()

        db.binance = type('B', (), {'fetch_pair_ohlcv': staticmethod(_fetch_pair)})()
        db._fetch_crypto_data = lambda symbol, days: pd.DataFrame()

        db._fetch_crypto_metric_data('XRPBTC', days=5)

        assert seen == ['XRPBTC']

    def test_falls_back_to_computed_ratio_when_native_empty(self):
        """If Binance has no data for the pair, the derived close/close ratio
        must still serve the chart rather than returning nothing."""
        db = self._make_db()
        db.binance = type('B', (), {
            'fetch_pair_ohlcv': staticmethod(lambda pair, days: pd.DataFrame())
        })()

        idx = pd.date_range(end=datetime.now().date(), periods=5, freq='D')
        alt = pd.DataFrame({'close': 3000.0}, index=idx)
        base = pd.DataFrame({'close': 100000.0}, index=idx)
        db._fetch_crypto_data = lambda symbol, days: alt if symbol == 'ETH' else base

        df = db._fetch_crypto_metric_data('ETHBTC', days=5)

        assert not df.empty
        assert df['close'].iloc[-1] == pytest.approx(0.03)


if __name__ == '__main__':
    unittest.main()
