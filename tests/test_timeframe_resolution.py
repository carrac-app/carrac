"""A timeframe must never silently become daily.

`_load_ohlcv_cached` maps each supported timeframe to its own CSV and refuses
the rest, so `15m`, `5m` or a typo can never come back as daily bars, and
`backtest_run` / `indicator_run_formula` at different timeframes never share
output.

These tests pin two properties: unsupported timeframes are REFUSED, and
distinct supported timeframes produce DISTINCT bars.
"""

import pytest

from carrac.formula_eval import (
    canonical_timeframe,
    load_ohlcv,
    SUPPORTED_TIMEFRAMES,
    _TF_SOURCE,
)


class TestCanonicalTimeframe:
    @pytest.mark.parametrize('tf', ['1h', '2h', '4h', '1D', '3D', '1W', '1M'])
    def test_supported_timeframes_round_trip(self, tf):
        assert canonical_timeframe(tf) == tf

    @pytest.mark.parametrize('given,expected', [
        ('1d', '1D'), ('3d', '3D'), ('1w', '1W'),
        ('1H', '1h'), ('4H', '4h'),
        ('  1D  ', '1D'),
    ])
    def test_unambiguous_aliases_are_folded(self, given, expected):
        assert canonical_timeframe(given) == expected

    @pytest.mark.parametrize('tf', ['15m', '5m', '30m', '7m', '1m', 'banana', '', '1y'])
    def test_unsupported_timeframes_are_refused(self, tf):
        assert canonical_timeframe(tf) is None, (
            f'{tf!r} must be refused, not silently resolved'
        )

    def test_lowercase_1m_is_not_folded_into_monthly(self):
        """'1m' is one MINUTE; '1M' is one MONTH. Folding them would serve
        monthly bars to a minute request — the exact silent-wrong-answer
        class this module exists to prevent."""
        assert canonical_timeframe('1M') == '1M'
        assert canonical_timeframe('1m') is None

    @pytest.mark.parametrize('bad', [None, 15, 1.5, ['1D']])
    def test_non_string_input_is_refused(self, bad):
        assert canonical_timeframe(bad) is None

    def test_supported_set_matches_source_map(self):
        assert set(SUPPORTED_TIMEFRAMES) == set(_TF_SOURCE)


@pytest.fixture
def seeded_btc_daily(tmp_path, monkeypatch):
    """Put a real BTC daily CSV under a throwaway cache dir.

    `data/` is not in the repo, so the two tests below only ever passed on a
    machine that had already fetched BTC bars. The first CI run that got as
    far as pytest failed them both on `assert (None is not None)` — CI had
    never reached pytest before that was fixed. Nothing is stubbed: this writes a
    real CSV and the loader reads, resamples and trims it exactly as in production.
    """
    import numpy as np
    import pandas as pd

    from csv_cache import CSVCache
    from carrac import formula_eval

    idx = pd.date_range('2024-01-01', periods=400, freq='D')
    close = np.linspace(40_000.0, 70_000.0, 400)
    frame = pd.DataFrame(
        {
            'open': close,
            'high': close * 1.01,
            'low': close * 0.99,
            'close': close,
            'volume': np.full(400, 1_000.0),
        },
        index=idx,
    )
    frame.index.name = 'date'

    cache = CSVCache(str(tmp_path))
    frame.to_csv(cache.get_path('BTC', 'daily'))

    monkeypatch.setattr(formula_eval, '_csv_cache', cache)
    formula_eval._load_ohlcv_cached.cache_clear()
    yield
    formula_eval._load_ohlcv_cached.cache_clear()


class TestLoadOhlcvRefusesUnsupported:
    @pytest.mark.parametrize('tf', ['15m', '5m', '1m', 'banana'])
    def test_returns_none_instead_of_daily(self, tf):
        assert load_ohlcv('BTC', tf) is None, (
            f'load_ohlcv fell back to daily for {tf!r} — the silent daily fallback bug'
        )

    @pytest.mark.usefixtures('seeded_btc_daily')
    def test_daily_still_loads(self):
        d = load_ohlcv('BTC', '1D')
        assert d is not None and len(d['close']) > 0

    @pytest.mark.usefixtures('seeded_btc_daily')
    def test_distinct_timeframes_give_distinct_bars(self):
        """Daily and weekly must
        not return identical series."""
        daily = load_ohlcv('BTC', '1D')
        weekly = load_ohlcv('BTC', '1W')
        assert daily is not None and weekly is not None
        assert len(daily['close']) != len(weekly['close']), (
            '1D and 1W returned the same bar count — resampling is not happening'
        )


class TestMcpSurfaceRejectsUnsupported:
    """The agent-facing surface must raise, not return a plausible number."""

    @pytest.mark.parametrize('tf', ['15m', '5m', '1m', 'banana'])
    def test_require_supported_timeframe_raises(self, tf):
        from carrac.mcp.protocol import ToolError
        from carrac.mcp.tools.indicators import _require_supported_timeframe

        with pytest.raises(ToolError) as exc:
            _require_supported_timeframe(tf)
        assert 'Unsupported timeframe' in str(exc.value)

    @pytest.mark.parametrize('tf', ['1D', '1h', '4h', '1W'])
    def test_supported_timeframes_pass(self, tf):
        from carrac.mcp.tools.indicators import _require_supported_timeframe
        _require_supported_timeframe(tf)  # must not raise
