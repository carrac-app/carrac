"""Tests for MCP tools in carrac/mcp/tools/charts.py:
  - chart_get_screenshot: agent-sane defaults + no-silent-truncation guard
  - chart_get_volume_profile: period_iso must never report epoch-garbage
    actual_start/actual_end when the underlying data has no real per-bar
    timestamps
"""

import os
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

from carrac.mcp import tools  # noqa: F401  ensures registration
from carrac.mcp.registry import TOOL_REGISTRY
from carrac.mcp.protocol import ToolError


def _cache_paths(asset, timeframe, width, height):
    cache_dir = os.path.join('data', 'screenshots')
    return cache_dir, os.path.join(cache_dir, f'{asset}_{timeframe}_{width}x{height}.jpg')


def _cleanup_cache(asset, timeframe, width, height):
    _, cache_file = _cache_paths(asset, timeframe, width, height)
    if os.path.exists(cache_file):
        os.remove(cache_file)


class TestGetChartScreenshotDefaults:
    """A default call (asset=BTC, default width/height/quality) must
    not return a ~123,700-char base64 payload that exceeded the MCP
    tool-response limit on the very first ordinary call."""

    def test_default_dimensions_are_small_enough_for_the_response_limit(self):
        import carrac.mcp.tools.charts as charts_mod
        assert charts_mod._DEFAULT_SCREENSHOT_WIDTH < 1280
        assert charts_mod._DEFAULT_SCREENSHOT_HEIGHT < 720
        assert charts_mod._DEFAULT_SCREENSHOT_QUALITY < 70

    def test_default_call_uses_small_dimensions(self):
        """Omitting width/height/quality must resolve to the small
        defaults, not 1280x720 q70."""
        import carrac.mcp.tools.charts as charts_mod
        asset, timeframe = 'ZZZTESTDEFAULT', '1D'
        w, h = charts_mod._DEFAULT_SCREENSHOT_WIDTH, charts_mod._DEFAULT_SCREENSHOT_HEIGHT
        _cleanup_cache(asset, timeframe, w, h)
        try:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.content = b'\xff\xd8\xff\xdb-fake-small-jpeg'
            with patch('requests.get', return_value=mock_resp) as mock_get:
                fn = TOOL_REGISTRY['chart_get_screenshot']['handler']
                out = fn({'asset': asset})
            assert out['width'] == w
            assert out['height'] == h
            call_kwargs = mock_get.call_args.kwargs
            assert call_kwargs['params']['width'] == w
            assert call_kwargs['params']['height'] == h
            assert call_kwargs['params']['quality'] == charts_mod._DEFAULT_SCREENSHOT_QUALITY
        finally:
            _cleanup_cache(asset, timeframe, w, h)

    def test_oversized_result_raises_instead_of_silently_truncating(self):
        """If the rendered image is still too large (e.g. explicit large
        width/height/quality), the tool must fail with an actionable error
        rather than returning a truncated (corrupted) base64 payload."""
        import carrac.mcp.tools.charts as charts_mod
        asset, timeframe = 'ZZZTESTBIG', '1D'
        w, h = 3840, 3840
        _cleanup_cache(asset, timeframe, w, h)
        try:
            # Raw bytes sized so the base64 encoding lands comfortably over
            # the guard threshold.
            big_bytes = b'\xff' * (charts_mod._MAX_SCREENSHOT_B64_CHARS)
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.content = big_bytes
            with patch('requests.get', return_value=mock_resp):
                fn = TOOL_REGISTRY['chart_get_screenshot']['handler']
                with pytest.raises(ToolError) as exc:
                    fn({'asset': asset, 'width': w, 'height': h, 'quality': 100})
            msg = str(exc.value).lower()
            assert 'too large' in msg
            assert 'width' in msg or 'height' in msg or 'quality' in msg
            # Must not have written a corrupted/truncated file to the cache.
            _, cache_file = _cache_paths(asset, timeframe, w, h)
            assert not os.path.exists(cache_file)
        finally:
            _cleanup_cache(asset, timeframe, w, h)

    def test_missing_asset_raises(self):
        fn = TOOL_REGISTRY['chart_get_screenshot']['handler']
        with pytest.raises(ToolError):
            fn({'asset': ''})


class _FakeDb:
    """Stub for get_services().db used by chart_get_volume_profile tests."""

    def __init__(self, df):
        self._df = df

    def fetch_klines_for_range(self, symbol, interval, start_ms, end_ms):
        return self._df


class _FakeServices:
    def __init__(self, df):
        self.db = _FakeDb(df)


def _fake_vp(*args, **kwargs):
    return {'bins': [], 'poc': 100.0, 'vah': 105.0, 'val': 95.0}


class TestGetVolumeProfilePeriodIso:
    """period_iso.actual_start/actual_end must not come back
    as Unix-epoch garbage (1970-01-01T00:00:00) when the underlying kline
    fetch returns a bare positional RangeIndex instead of real per-bar
    timestamps (data_sources/binance.py's fetch_klines_for_range drops
    open_time instead of indexing on it). requested_start/end must stay
    correct either way."""

    def _make_range_indexed_df(self, n=50):
        """A DataFrame shaped like what fetch_klines_for_range actually
        returns for a range query: OHLCV columns, default RangeIndex(0..n-1),
        NOT a DatetimeIndex."""
        return pd.DataFrame({
            'open': np.linspace(100, 110, n),
            'high': np.linspace(101, 111, n),
            'low': np.linspace(99, 109, n),
            'close': np.linspace(100.5, 110.5, n),
            'volume': np.full(n, 10.0),
            'taker_buy_base': np.full(n, 5.0),
        })  # index is a plain RangeIndex — no datetime information at all

    def _make_properly_indexed_df(self, n=50):
        idx = pd.date_range('2026-06-01', periods=n, freq='1h', tz='UTC')
        return pd.DataFrame({
            'open': np.linspace(100, 110, n),
            'high': np.linspace(101, 111, n),
            'low': np.linspace(99, 109, n),
            'close': np.linspace(100.5, 110.5, n),
            'volume': np.full(n, 10.0),
            'taker_buy_base': np.full(n, 5.0),
        }, index=idx)

    def test_range_indexed_df_does_not_report_epoch_garbage(self, monkeypatch):
        df = self._make_range_indexed_df()
        monkeypatch.setattr(
            'carrac.services.get_services', lambda: _FakeServices(df))
        with patch('volume_profile.compute_volume_profile', side_effect=_fake_vp):
            fn = TOOL_REGISTRY['chart_get_volume_profile']['handler']
            out = fn({'symbol': 'BTC', 'lookback_days': 5})

        period = out['period_iso']
        assert period['requested_start'] is not None
        assert period['requested_end'] is not None
        # These must not silently become '1970-01-01T...'.
        assert period['actual_start'] is None
        assert period['actual_end'] is None
        assert 'note' in period
        assert '1970' not in str(period)

    def test_properly_indexed_df_reports_real_actual_bounds(self, monkeypatch):
        df = self._make_properly_indexed_df()
        monkeypatch.setattr(
            'carrac.services.get_services', lambda: _FakeServices(df))
        with patch('volume_profile.compute_volume_profile', side_effect=_fake_vp):
            fn = TOOL_REGISTRY['chart_get_volume_profile']['handler']
            out = fn({'symbol': 'BTC', 'lookback_days': 5})

        period = out['period_iso']
        assert period['actual_start'] is not None
        assert period['actual_end'] is not None
        assert '1970' not in period['actual_start']
        assert '2026-06-01' in period['actual_start']
        assert 'note' not in period

    def test_missing_symbol_raises(self):
        fn = TOOL_REGISTRY['chart_get_volume_profile']['handler']
        with pytest.raises(ToolError):
            fn({'symbol': ''})
