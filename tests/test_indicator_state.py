"""
Unit tests for the Indicator State Service (carrac/indicator_state.py).

Covers: format_matrix_row, get_matrix_data,
        extract_fingerprint, detect_transitions, check_indicator_alerts.
"""

from carrac.indicator_state import (
    format_matrix_row, get_matrix_data,
    extract_fingerprint, detect_transitions, INDICATOR_ALERT_CONDITIONS,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_asset(symbol='BTC', **overrides):
    """Build a minimal asset dict matching _compute_assets_response output."""
    asset = {
        'symbol': symbol,
        'name': 'Bitcoin',
        'type': 'crypto',
        'category': 'crypto',
        'latest_price': 50000.0,
        'realtime': {'price': 50100.0, 'change_pct': 1.5, 'is_live': True},
        'ema_ribbon': {'trend': 'bullish', 'days_since_flip': 12, 'position': 'above'},
        'weekly_rsi': 62.5,
        'bb': {'upper': 52000, 'middle': 50000, 'lower': 48000, 'width_pct': 8.0, 'squeeze': False},
        'consensio': {
            'total': {'signal': 2, 'positions': 4, 'max_positions': 6},
            '1D': {'signal': 1, 'positions': 2},
            '3D': {'signal': 1, 'positions': 1},
            '1W': {'signal': 0, 'positions': 1},
        },
        'structure': {'structure_trend': 'uptrend', 'last_high_type': 'HH', 'last_low_type': 'HL'},
        'macd': {'histogram': 150.5, 'signal': 200, 'value': 350.5},
        'adx': {'adx': 35.2, 'plus_di': 28.0, 'minus_di': 15.0, 'regime': 'strong_trend',
                'is_rising': True, 'trend_direction': 'bullish', 'atr': 1200.0, 'atr_pct': 2.4},
        'cpr': {'pivot': 49500, 'tc': 49800, 'bc': 49200, 's1': 48500, 'r1': 50500,
                'price_level': 'CP-TC'},
        'volume_metrics': {'volume_ma_20': 5000.0, 'current_volume': 7500.0, 'ratio': 1.5, 'is_above_average': True},
    }
    asset.update(overrides)
    return asset


class FakeCache:
    """Fake response cache for testing."""
    def __init__(self, htf_assets=None, ltf_assets=None):
        self._data = {}
        if htf_assets is not None:
            self._data['htf'] = {'assets': htf_assets}
        if ltf_assets is not None:
            self._data['ltf'] = {'assets': ltf_assets}

    def get(self, mode):
        return self._data.get(mode)


# ---------------------------------------------------------------------------
# format_matrix_row
# ---------------------------------------------------------------------------

class TestFormatMatrixRow:
    def test_basic_fields(self):
        row = format_matrix_row(_make_asset())
        assert row['symbol'] == 'BTC'
        assert row['name'] == 'Bitcoin'
        assert row['price'] == 50100.0  # realtime price used
        assert row['change_pct'] == 1.5

    def test_ema_ribbon(self):
        row = format_matrix_row(_make_asset())
        assert row['ema_ribbon']['trend'] == 'bullish'
        assert row['ema_ribbon']['days'] == 12

    def test_rsi(self):
        row = format_matrix_row(_make_asset())
        assert row['rsi']['value'] == 62.5
        assert row['rsi']['zone'] == 'neutral'

    def test_rsi_oversold(self):
        row = format_matrix_row(_make_asset(weekly_rsi=25.0))
        assert row['rsi']['zone'] == 'oversold'

    def test_rsi_overbought(self):
        row = format_matrix_row(_make_asset(weekly_rsi=75.0))
        assert row['rsi']['zone'] == 'overbought'

    def test_rsi_none(self):
        row = format_matrix_row(_make_asset(weekly_rsi=None))
        assert row['rsi']['value'] is None
        assert row['rsi']['zone'] is None

    def test_bb_squeeze(self):
        row = format_matrix_row(_make_asset(bb={'squeeze': True, 'width_pct': 3.5}))
        assert row['bb_squeeze']['squeezed'] is True
        assert row['bb_squeeze']['width_pct'] == 3.5

    def test_bb_squeeze_pct_exposed(self):
        # squeeze_pct (band width vs 60-day avg) is the metric the web colours by;
        # thin clients (Carrac TUI) need it to render the same BB column.
        row = format_matrix_row(_make_asset(bb={'squeeze': True, 'width_pct': 3.5, 'squeeze_pct': 42.0}))
        assert row['bb_squeeze']['squeeze_pct'] == 42.0

    def test_bb_squeeze_pct_none_when_absent(self):
        row = format_matrix_row(_make_asset(bb={'squeeze': False, 'width_pct': 80.0}))
        assert row['bb_squeeze']['squeeze_pct'] is None

    def test_consensio(self):
        row = format_matrix_row(_make_asset())
        assert row['consensio']['signal'] == 2
        assert row['consensio']['positions'] == 4

    def test_structure(self):
        row = format_matrix_row(_make_asset())
        assert row['structure']['trend'] == 'uptrend'
        assert row['structure']['label'] == 'HH/HL'

    def test_macd_bullish(self):
        row = format_matrix_row(_make_asset())
        assert row['macd']['direction'] == 'bullish'

    def test_macd_bearish(self):
        row = format_matrix_row(_make_asset(macd={'histogram': -50}))
        assert row['macd']['direction'] == 'bearish'

    def test_adx(self):
        row = format_matrix_row(_make_asset())
        assert row['adx']['value'] == 35.2
        assert row['adx']['regime'] == 'strong_trend'

    def test_cpr_above(self):
        row = format_matrix_row(_make_asset())
        assert row['cpr']['position'] == 'above'

    def test_cpr_below(self):
        asset = _make_asset(latest_price=49000.0)
        asset['realtime'] = None
        row = format_matrix_row(asset)
        assert row['cpr']['position'] == 'below'

    def test_cpr_price_level_exposed(self):
        # The fine CPR zone (e.g. "CP-TC"/"TC-R1") must reach the matrix row so
        # thin clients (Carrac TUI) can render the same CPR column as the web.
        row = format_matrix_row(_make_asset())
        assert row['cpr']['price_level'] == 'CP-TC'

    def test_cpr_price_level_none_when_absent(self):
        asset = _make_asset()
        asset['cpr'] = {'pivot': 49500}  # no price_level key
        row = format_matrix_row(asset)
        assert row['cpr']['price_level'] is None

    def test_atr_pct(self):
        row = format_matrix_row(_make_asset())
        assert row['atr_pct']['value'] == 2.4

    def test_rel_volume(self):
        row = format_matrix_row(_make_asset())
        assert row['rel_volume']['ratio'] == 1.5
        assert row['rel_volume']['above_avg'] is True

    def test_missing_fields_graceful(self):
        """Minimal asset with missing data should not crash."""
        row = format_matrix_row({'symbol': 'XYZ', 'latest_price': 10.0})
        assert row['symbol'] == 'XYZ'
        assert row['rsi']['value'] is None
        assert row['adx']['value'] is None


# ---------------------------------------------------------------------------
# get_matrix_data
# ---------------------------------------------------------------------------

class TestGetMatrixData:
    def test_returns_rows(self):
        cache = FakeCache(htf_assets=[_make_asset('BTC'), _make_asset('ETH')])
        rows = get_matrix_data(cache)
        assert len(rows) == 2
        symbols = [r['symbol'] for r in rows]
        assert 'BTC' in symbols
        assert 'ETH' in symbols

    def test_filters_metrics(self):
        cache = FakeCache(htf_assets=[
            _make_asset('BTC'),
            _make_asset('TOTAL', type='crypto_metric'),
        ])
        rows = get_matrix_data(cache)
        assert len(rows) == 1
        assert rows[0]['symbol'] == 'BTC'

    def test_empty_cache(self):
        cache = FakeCache()
        assert get_matrix_data(cache) == []


# ---------------------------------------------------------------------------
# extract_fingerprint + detect_transitions
# ---------------------------------------------------------------------------

class TestFingerprint:
    def test_extract_basic(self):
        fp = extract_fingerprint(_make_asset())
        assert fp['ema_ribbon'] == 'bullish'
        assert fp['rsi_zone'] == 'neutral'  # RSI 62.5 → neutral (3-zone vocabulary)
        assert fp['bb_squeeze'] == 'expanded'  # width_pct=8.0 > 6 and squeeze=False
        assert fp['consensio'] == 'bull'  # named bucket (positions/max ratio) — matches Plays vocab
        assert fp['structure'] == 'uptrend'  # structure_trend passes through
        assert fp['macd'] == 'bullish'
        assert fp['adx_regime'] == 'strong_trend'
        assert fp['cpr_position'] == 'above_pivot'  # price_level 'CP-TC' → above_pivot

    def test_extract_bearish(self):
        asset = _make_asset(
            ema_ribbon={'trend': 'bearish'},
            weekly_rsi=25.0,
            macd={'histogram': -50},
        )
        fp = extract_fingerprint(asset)
        assert fp['ema_ribbon'] == 'bearish'
        assert fp['rsi_zone'] == 'oversold'
        assert fp['macd'] == 'bearish'

    def test_extract_missing_data(self):
        fp = extract_fingerprint({'symbol': 'XYZ'})
        assert fp['ema_ribbon'] == 'neutral'
        assert fp['rsi_zone'] is None


class TestDetectTransitions:
    def test_no_change(self):
        fp = {'ema_ribbon': 'bullish', 'macd': 'bullish'}
        events = detect_transitions(fp, fp, 'BTC')
        assert events == []

    def test_ema_flip(self):
        prev = {'ema_ribbon': 'bearish', 'macd': 'bearish'}
        curr = {'ema_ribbon': 'bullish', 'macd': 'bearish'}
        events = detect_transitions(curr, prev, 'BTC')
        assert len(events) == 1
        assert events[0]['indicator'] == 'ema_ribbon'
        assert events[0]['from'] == 'bearish'
        assert events[0]['to'] == 'bullish'
        assert events[0]['symbol'] == 'BTC'
        assert events[0]['significance'] == 3

    def test_multiple_transitions(self):
        prev = {'ema_ribbon': 'bearish', 'macd': 'bearish', 'bb_squeeze': False}
        curr = {'ema_ribbon': 'bullish', 'macd': 'bullish', 'bb_squeeze': True}
        events = detect_transitions(curr, prev, 'ETH')
        assert len(events) == 3
        indicators = {e['indicator'] for e in events}
        assert indicators == {'ema_ribbon', 'macd', 'bb_squeeze'}

    def test_no_previous(self):
        curr = {'ema_ribbon': 'bullish'}
        assert detect_transitions(curr, None, 'BTC') == []
        assert detect_transitions(curr, {}, 'BTC') == []

    def test_none_values_skipped(self):
        prev = {'ema_ribbon': None, 'macd': 'bearish'}
        curr = {'ema_ribbon': 'bullish', 'macd': 'bullish'}
        events = detect_transitions(curr, prev, 'BTC')
        # ema_ribbon: prev is None → skipped
        # macd: bearish → bullish → detected
        assert len(events) == 1
        assert events[0]['indicator'] == 'macd'


class TestIndicatorAlertConditions:
    def test_valid_indicators(self):
        # Spot-check the well-known ones; the registry grows over time as
        # new indicators are added so we don't pin a hard count.
        for key in ('ema_ribbon', 'rsi_zone', 'bb_squeeze'):
            assert key in INDICATOR_ALERT_CONDITIONS
        assert len(INDICATOR_ALERT_CONDITIONS) >= 8


class TestMatrixRouteApiKey:
    """GET /api/details/matrix resolves the per-user API-key owner and
    filters to THEIR watchlist. Reading session['user_id'] only would give
    key callers (the Carrac TUI) the full unfiltered universe.
    """

    def test_api_key_caller_gets_watchlist_filtered_rows(self, client, monkeypatch):
        from flask import g
        from carrac.auth import decorators as auth_dec
        import carrac.indicator_state as ind_state
        from user_db import user_db

        original_verify = auth_dec.verify_api_key

        def _verify(key):
            if not original_verify(key):
                return False
            g.api_key_user_id = 77
            g.api_key_tier = 'premium'
            return True

        # Matrix universe = 3 assets; the key holder's watchlist is 2 → DOGE drops.
        rows = [
            {'symbol': 'BTC', 'cpr': {'price_level': 'TC-R1'}},
            {'symbol': 'ETH', 'cpr': {'price_level': 'CP-TC'}},
            {'symbol': 'DOGE', 'cpr': {'price_level': 'Below S3'}},
        ]
        monkeypatch.setattr(auth_dec, 'verify_api_key', _verify)
        monkeypatch.setattr(ind_state, 'get_matrix_data', lambda *_a, **_k: [dict(r) for r in rows])
        monkeypatch.setattr(user_db, 'get_flat_watchlist',
                            lambda uid: ['BTC', 'ETH'] if uid == 77 else [])

        import os
        api_key = os.environ.get('SCREENSHOT_API_KEY', 'test-api-key-for-testing')
        resp = client.get('/api/details/matrix',
                          headers={'X-Internal-Key': api_key, 'Accept': 'application/json'})
        assert resp.status_code == 200
        data = resp.get_json()
        symbols = {r['symbol'] for r in data['rows']}
        assert symbols == {'BTC', 'ETH'}  # DOGE excluded by the watchlist filter
        # cpr.price_level (the TC-R1 fine zone) survives end-to-end.
        btc = next(r for r in data['rows'] if r['symbol'] == 'BTC')
        assert btc['cpr']['price_level'] == 'TC-R1'
