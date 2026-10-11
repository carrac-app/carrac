"""
Parity tests for PSAR and ICHIMOKU_TK indicator state wiring.

These tests verify:
  (a) conditions._get_indicator_state returns correct states for PSAR
  (b) conditions._get_indicator_state returns correct states for ICHIMOKU_TK
  (c) PARITY: for the same asset dict, conditions._get_indicator_state produces
      the same result as TRACKED_STATES — the guard for the
      "wire-in-two-places" class of bug.
"""

import pytest
from carrac.conditions import _get_indicator_state
from carrac.indicator_state import TRACKED_STATES


# ── Asset data factories ──

def _psar_asset(direction):
    """Build a minimal asset dict with the given PSAR direction ('long'/'short'/None)."""
    d = {'cockpit_extended': {'psar': {}}}
    if direction is not None:
        d['cockpit_extended']['psar']['direction'] = direction
    return d


def _ichimoku_tk_asset(tk_cross):
    """Build a minimal asset dict with the given Ichimoku TK cross value."""
    d = {'cockpit_extended': {'ichimoku': {}}}
    if tk_cross is not None:
        d['cockpit_extended']['ichimoku']['tk_cross'] = tk_cross
    return d


# ── TRACKED_STATES lookup helpers ──

def _tracked_fn(key):
    """Return the extraction function registered for `key` in TRACKED_STATES."""
    for k, fn, _ in TRACKED_STATES:
        if k == key:
            return fn
    raise KeyError(f"'{key}' not found in TRACKED_STATES")


# ══════════════════════════════════════════════════════════════════════════════
# (a) PSAR via conditions._get_indicator_state
# ══════════════════════════════════════════════════════════════════════════════

class TestPsarIndicatorState:
    """_get_indicator_state('PSAR', ...) returns the correct state strings."""

    def test_psar_long_returns_bullish(self):
        asset = _psar_asset('long')
        assert _get_indicator_state('PSAR', asset) == 'bullish'

    def test_psar_short_returns_bearish(self):
        asset = _psar_asset('short')
        assert _get_indicator_state('PSAR', asset) == 'bearish'

    def test_psar_missing_direction_returns_none(self):
        asset = _psar_asset(None)
        assert _get_indicator_state('PSAR', asset) is None

    def test_psar_missing_cockpit_returns_none(self):
        assert _get_indicator_state('PSAR', {}) is None

    def test_psar_unknown_direction_returns_none(self):
        asset = _psar_asset('sideways')
        assert _get_indicator_state('PSAR', asset) is None

    def test_psar_case_insensitive_key(self):
        """The indicator name lookup normalises to upper-case."""
        asset = _psar_asset('long')
        assert _get_indicator_state('psar', asset) == 'bullish'


# ══════════════════════════════════════════════════════════════════════════════
# (b) ICHIMOKU_TK via conditions._get_indicator_state
# ══════════════════════════════════════════════════════════════════════════════

class TestIchimokuTkIndicatorState:
    """_get_indicator_state('ICHIMOKU_TK', ...) returns the correct state strings."""

    def test_ichimoku_tk_bullish(self):
        asset = _ichimoku_tk_asset('bullish')
        assert _get_indicator_state('ICHIMOKU_TK', asset) == 'bullish'

    def test_ichimoku_tk_bearish(self):
        asset = _ichimoku_tk_asset('bearish')
        assert _get_indicator_state('ICHIMOKU_TK', asset) == 'bearish'

    def test_ichimoku_tk_none_between_crosses(self):
        """'none' is a valid state meaning "no cross on this bar"."""
        asset = _ichimoku_tk_asset('none')
        assert _get_indicator_state('ICHIMOKU_TK', asset) == 'none'

    def test_ichimoku_tk_missing_field_returns_none(self):
        asset = _ichimoku_tk_asset(None)
        assert _get_indicator_state('ICHIMOKU_TK', asset) is None

    def test_ichimoku_tk_missing_cockpit_returns_none(self):
        assert _get_indicator_state('ICHIMOKU_TK', {}) is None

    def test_ichimoku_tk_case_insensitive_key(self):
        asset = _ichimoku_tk_asset('bullish')
        assert _get_indicator_state('ichimoku_tk', asset) == 'bullish'


# ══════════════════════════════════════════════════════════════════════════════
# (c) PARITY: conditions._get_indicator_state == TRACKED_STATES fn
# ══════════════════════════════════════════════════════════════════════════════

class TestPsarParity:
    """TRACKED_STATES['psar'] and conditions._get_indicator_state('PSAR') are identical."""

    def setup_method(self):
        self.fn = _tracked_fn('psar')

    @pytest.mark.parametrize('direction,expected', [
        ('long', 'bullish'),
        ('short', 'bearish'),
        (None, None),
    ])
    def test_parity(self, direction, expected):
        asset = _psar_asset(direction)
        assert self.fn(asset) == expected
        assert _get_indicator_state('PSAR', asset) == expected

    def test_parity_missing_cockpit(self):
        asset = {}
        assert self.fn(asset) == _get_indicator_state('PSAR', asset)


class TestIchimokuTkParity:
    """TRACKED_STATES['ichimoku_tk'] and conditions._get_indicator_state('ICHIMOKU_TK') are identical."""

    def setup_method(self):
        self.fn = _tracked_fn('ichimoku_tk')

    @pytest.mark.parametrize('tk_cross,expected', [
        ('bullish', 'bullish'),
        ('bearish', 'bearish'),
        ('none', 'none'),
        (None, None),
    ])
    def test_parity(self, tk_cross, expected):
        asset = _ichimoku_tk_asset(tk_cross)
        assert self.fn(asset) == expected
        assert _get_indicator_state('ICHIMOKU_TK', asset) == expected

    def test_parity_missing_cockpit(self):
        asset = {}
        assert self.fn(asset) == _get_indicator_state('ICHIMOKU_TK', asset)
