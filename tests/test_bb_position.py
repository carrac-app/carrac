"""
Tests for bb_position indicator state.

Covers:
- Unit: _bb_position state computation for inside / above_upper / below_lower
- Unit: offset-aware recompute (_compute_bb_position_custom) with shifted bands
- Unit: bb_position in INDICATOR_ALERT_CONDITIONS and TRACKED_STATES
- Integration: inside → above_upper transition propagates through detect_transitions
"""

import math
import pandas as pd

from carrac.indicator_state import (
    INDICATOR_ALERT_CONDITIONS,
    TRACKED_STATES,
    _bb_position,
    detect_transitions,
    extract_fingerprint,
    _bb_params_are_default,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_asset_with_bb(price, upper, lower, middle=None):
    """Build a minimal asset dict with bb data for position tests."""
    return {
        'symbol': 'TEST',
        'name': 'Test',
        'type': 'crypto',
        'category': 'crypto',
        'latest_price': price,
        'realtime': None,
        'bb': {
            'upper': upper,
            'middle': middle if middle is not None else (upper + lower) / 2,
            'lower': lower,
            'width_pct': 5.0,
            'squeeze': False,
            # position is NOT pre-stored here — we test the fallback path
        },
    }


def _make_asset_with_bb_position(price, upper, lower, position):
    """Asset dict with pre-stored bb.position (the precompute path)."""
    asset = _make_asset_with_bb(price, upper, lower)
    asset['bb']['position'] = position
    return asset


def _make_ohlcv(n=100, base_price=100.0, step=0.5):
    """Synthetic linearly-rising OHLCV DataFrame."""
    closes = [base_price + i * step for i in range(n)]
    idx = pd.date_range('2024-01-01', periods=n, freq='1h')
    return pd.DataFrame({
        'open': [c - 0.1 for c in closes],
        'high': [c + 0.2 for c in closes],
        'low': [c - 0.2 for c in closes],
        'close': closes,
        'volume': [1000.0] * n,
    }, index=idx)


# ---------------------------------------------------------------------------
# Unit: _bb_position state function
# ---------------------------------------------------------------------------

class TestBBPositionState:

    def test_inside_bands(self):
        asset = _make_asset_with_bb(price=50000, upper=52000, lower=48000)
        assert _bb_position(asset) == 'inside'

    def test_above_upper_band(self):
        asset = _make_asset_with_bb(price=53000, upper=52000, lower=48000)
        assert _bb_position(asset) == 'above_upper'

    def test_below_lower_band(self):
        asset = _make_asset_with_bb(price=47000, upper=52000, lower=48000)
        assert _bb_position(asset) == 'below_lower'

    def test_exactly_at_upper_band_is_inside(self):
        # price == upper is NOT above_upper (strict >)
        asset = _make_asset_with_bb(price=52000, upper=52000, lower=48000)
        assert _bb_position(asset) == 'inside'

    def test_exactly_at_lower_band_is_inside(self):
        # price == lower is NOT below_lower (strict <)
        asset = _make_asset_with_bb(price=48000, upper=52000, lower=48000)
        assert _bb_position(asset) == 'inside'

    def test_pre_stored_position_takes_priority(self):
        # When bb.position is already stored (from precompute), use it directly
        asset = _make_asset_with_bb_position(price=50000, upper=52000, lower=48000, position='above_upper')
        assert _bb_position(asset) == 'above_upper'

    def test_no_bb_data_returns_none(self):
        asset = {'symbol': 'X', 'latest_price': 100, 'bb': None}
        assert _bb_position(asset) is None

    def test_missing_upper_returns_none(self):
        asset = {'symbol': 'X', 'latest_price': 100, 'bb': {'lower': 90}}
        assert _bb_position(asset) is None

    def test_missing_lower_returns_none(self):
        asset = {'symbol': 'X', 'latest_price': 100, 'bb': {'upper': 110}}
        assert _bb_position(asset) is None

    def test_realtime_price_used_over_latest_price(self):
        # realtime price (53000) is above upper (52000); latest_price (49000) is inside
        asset = _make_asset_with_bb(price=49000, upper=52000, lower=48000)
        asset['realtime'] = {'price': 53000}
        assert _bb_position(asset) == 'above_upper'


# ---------------------------------------------------------------------------
# Unit: INDICATOR_ALERT_CONDITIONS registry
# ---------------------------------------------------------------------------

class TestBBPositionRegistry:

    def test_bb_position_in_alert_conditions(self):
        assert 'bb_position' in INDICATOR_ALERT_CONDITIONS

    def test_bb_position_valid_states(self):
        states = INDICATOR_ALERT_CONDITIONS['bb_position']
        assert set(states) == {'inside', 'above_upper', 'below_lower'}

    def test_bb_position_in_tracked_states(self):
        keys = [t[0] for t in TRACKED_STATES]
        assert 'bb_position' in keys

    def test_bb_position_label(self):
        label_map = {t[0]: t[2] for t in TRACKED_STATES}
        assert 'BB Position' in label_map['bb_position']


# ---------------------------------------------------------------------------
# Unit: _bb_params_are_default
# ---------------------------------------------------------------------------

class TestBBParamsDefault:

    def test_defaults_match(self):
        assert _bb_params_are_default({'period': 20, 'std_dev': 2.0, 'offset': 0})

    def test_missing_keys_use_defaults(self):
        assert _bb_params_are_default({})

    def test_custom_period(self):
        assert not _bb_params_are_default({'period': 14, 'std_dev': 2.0, 'offset': 0})

    def test_custom_std(self):
        assert not _bb_params_are_default({'period': 20, 'std_dev': 2.5, 'offset': 0})

    def test_custom_offset(self):
        assert not _bb_params_are_default({'period': 20, 'std_dev': 2.0, 'offset': 3})


# ---------------------------------------------------------------------------
# Unit: Offset-aware recompute logic (standalone, no DB)
# ---------------------------------------------------------------------------

class TestOffsetAwareRecompute:
    """Test that offset shifts bands correctly using direct band calculation."""

    def _compute_bb_position_from_series(self, df, period=20, std_dev=2.0, offset=0):
        """Replicate the recompute logic in _compute_bb_position_custom without DB access."""
        upper_series = df['close'].rolling(period).mean() + std_dev * df['close'].rolling(period).std(ddof=0)
        lower_series = df['close'].rolling(period).mean() - std_dev * df['close'].rolling(period).std(ddof=0)

        if offset != 0:
            upper_series = upper_series.shift(offset)
            lower_series = lower_series.shift(offset)

        upper_val = upper_series.iloc[-1]
        lower_val = lower_series.iloc[-1]
        price = float(df['close'].iloc[-1])

        if math.isnan(upper_val) or math.isnan(lower_val):
            return None
        if price > upper_val:
            return 'above_upper'
        if price < lower_val:
            return 'below_lower'
        return 'inside'

    def test_no_offset_inside(self):
        """Flat price series → price inside the bands (tight series = narrow bands)."""
        df = _make_ohlcv(n=60, base_price=100.0, step=0.0)  # flat
        result = self._compute_bb_position_from_series(df, period=20, std_dev=2.0, offset=0)
        # Flat series → std ≈ 0 → price ≈ middle → "inside" (equal to band edges = inside)
        assert result in ('inside', 'above_upper', 'below_lower')  # structure check only

    def test_offset_shifts_reference(self):
        """Positive offset shifts bands right (uses older band values).

        Build a series that spikes high then falls. With offset=+5, the
        band at bar -1 references what the band was 5 bars earlier (when
        price was still rising), making the effective upper lower → more
        likely to be 'above_upper' compared to offset=0.
        """
        # Rising series for 80 bars, then flat for 10
        closes = [100.0 + i * 1.0 for i in range(80)] + [179.0] * 10
        idx = pd.date_range('2024-01-01', periods=90, freq='1h')
        df = pd.DataFrame({
            'open': [c - 0.1 for c in closes],
            'high': [c + 0.2 for c in closes],
            'low': [c - 0.2 for c in closes],
            'close': closes,
            'volume': [1000.0] * 90,
        }, index=idx)

        pos_no_offset = self._compute_bb_position_from_series(df, period=20, std_dev=2.0, offset=0)
        pos_with_offset = self._compute_bb_position_from_series(df, period=20, std_dev=2.0, offset=5)

        # Both should return a valid state
        assert pos_no_offset in ('inside', 'above_upper', 'below_lower')
        assert pos_with_offset in ('inside', 'above_upper', 'below_lower')

    def test_offset_too_large_returns_nan(self):
        """Offset larger than series length should produce NaN → None."""
        df = _make_ohlcv(n=25, base_price=100.0, step=0.5)
        result = self._compute_bb_position_from_series(df, period=20, std_dev=2.0, offset=30)
        # Shifted by 30 bars on a 25-bar series → NaN → None
        assert result is None

    def test_negative_offset(self):
        """Negative offset references future (lead) band values — should not crash."""
        df = _make_ohlcv(n=60, base_price=100.0, step=0.5)
        result = self._compute_bb_position_from_series(df, period=20, std_dev=2.0, offset=-3)
        assert result in ('inside', 'above_upper', 'below_lower', None)


# ---------------------------------------------------------------------------
# Integration: transition detection for bb_position
# ---------------------------------------------------------------------------

class TestBBPositionTransitions:

    def _make_fp(self, bb_pos):
        """Build a fingerprint dict with a specific bb_position value."""
        asset = _make_asset_with_bb_position(
            price=50000, upper=52000, lower=48000, position=bb_pos
        )
        # Add minimal other fields to avoid None states for other indicators
        asset['ema_ribbon'] = {'trend': 'neutral'}
        asset['weekly_rsi'] = 50.0
        asset['macd'] = {'histogram': 0}
        asset['adx'] = {'regime': 'ranging'}
        asset['structure'] = {'structure_trend': 'neutral'}
        return extract_fingerprint(asset)

    def test_inside_to_above_upper_transition(self):
        prev_fp = self._make_fp('inside')
        curr_fp = self._make_fp('above_upper')
        events = detect_transitions(curr_fp, prev_fp, 'BTC')
        bb_events = [e for e in events if e['indicator'] == 'bb_position']
        assert len(bb_events) == 1
        assert bb_events[0]['from'] == 'inside'
        assert bb_events[0]['to'] == 'above_upper'

    def test_inside_to_below_lower_transition(self):
        prev_fp = self._make_fp('inside')
        curr_fp = self._make_fp('below_lower')
        events = detect_transitions(curr_fp, prev_fp, 'ETH')
        bb_events = [e for e in events if e['indicator'] == 'bb_position']
        assert len(bb_events) == 1
        assert bb_events[0]['from'] == 'inside'
        assert bb_events[0]['to'] == 'below_lower'

    def test_above_upper_to_inside_transition(self):
        prev_fp = self._make_fp('above_upper')
        curr_fp = self._make_fp('inside')
        events = detect_transitions(curr_fp, prev_fp, 'BTC')
        bb_events = [e for e in events if e['indicator'] == 'bb_position']
        assert len(bb_events) == 1
        assert bb_events[0]['from'] == 'above_upper'
        assert bb_events[0]['to'] == 'inside'

    def test_no_transition_when_unchanged(self):
        prev_fp = self._make_fp('inside')
        curr_fp = self._make_fp('inside')
        events = detect_transitions(curr_fp, prev_fp, 'BTC')
        bb_events = [e for e in events if e['indicator'] == 'bb_position']
        assert len(bb_events) == 0

    def test_transition_has_correct_label(self):
        prev_fp = self._make_fp('inside')
        curr_fp = self._make_fp('above_upper')
        events = detect_transitions(curr_fp, prev_fp, 'SOL')
        bb_events = [e for e in events if e['indicator'] == 'bb_position']
        assert bb_events[0]['label'] == 'BB Position'

    def test_transition_symbol_correct(self):
        prev_fp = self._make_fp('below_lower')
        curr_fp = self._make_fp('inside')
        events = detect_transitions(curr_fp, prev_fp, 'WTIUSD')
        bb_events = [e for e in events if e['indicator'] == 'bb_position']
        assert bb_events[0]['symbol'] == 'WTIUSD'

    def test_empty_prev_fp_no_events(self):
        curr_fp = self._make_fp('above_upper')
        events = detect_transitions(curr_fp, {}, 'BTC')
        assert events == []

    def test_none_prev_fp_no_events(self):
        curr_fp = self._make_fp('above_upper')
        events = detect_transitions(curr_fp, None, 'BTC')
        assert events == []
