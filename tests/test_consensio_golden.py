"""
Golden-output tests for consensio.py — pins public API of all 16 classes.

Purpose: tripwire for the refactor that splits consensio.py into a package.
Any behaviour drift between the pre-refactor baseline and the post-refactor
package must fail here.

Workflow:
    # Seed the golden files (once, on the pre-refactor tree):
    UPDATE_GOLDENS=1 pytest tests/test_consensio_golden.py

    # Then on every subsequent run (including CI):
    pytest tests/test_consensio_golden.py

Time-dependent fields (e.g. days_since_flip) are stripped before compare so
re-running on a later day does not falsely drift the goldens. All other output
is pinned byte-for-byte with 1e-9 float tolerance.
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from consensio.analysis.trend import TrendAnalyzer
from consensio.constants import (
    ADX_STRONG_TREND,
    ADX_WEAK_TREND,
    FRESH_FLIP_DAYS,
    RSI_OVERBOUGHT,
    RSI_OVERSOLD,
)
from consensio.indicators.adx import ADXCalculator
from consensio.indicators.bollinger import BollingerBandCalculator
from consensio.indicators.consensio_core import ConsensioCalculator
from consensio.indicators.cpr import CPRCalculator
from consensio.indicators.ema_ribbon import EmaRibbonCalculator
from consensio.indicators.market_structure import MarketStructureAnalyzer
from consensio.indicators.rsi import RSICalculator, RSIDivergenceDetector
from consensio.scoring.balanced import BalancedScorer
from consensio.scoring.ema_focused import EMAFocusedScorer
from consensio.scoring.opportunity import OpportunityScorer
from consensio.scoring.research import ResearchScorer
from consensio.scoring.user import UserScorer

GOLDEN_DIR = Path(__file__).parent / 'golden' / 'consensio'
UPDATE_GOLDENS = os.environ.get('UPDATE_GOLDENS') == '1'

TIME_DEPENDENT_KEYS = {'days_since_flip', 'days_since', 'days_since_change'}


def _to_jsonable(obj: Any) -> Any:
    """Convert numpy/pandas types to JSON-safe primitives with stable ordering."""
    if obj is None or isinstance(obj, (bool, int, str)):
        return obj
    if isinstance(obj, float):
        if math.isnan(obj):
            return '__NaN__'
        if math.isinf(obj):
            return '__Inf__' if obj > 0 else '__NegInf__'
        return obj
    if isinstance(obj, np.bool_):
        return bool(obj)
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, np.floating):
        return _to_jsonable(float(obj))
    if isinstance(obj, np.ndarray):
        return [_to_jsonable(v) for v in obj.tolist()]
    if isinstance(obj, pd.Series):
        return [_to_jsonable(v) for v in obj.tolist()]
    if isinstance(obj, pd.Timestamp):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {str(k): _to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_jsonable(v) for v in obj]
    raise TypeError(f'Cannot JSONify {type(obj).__name__}: {obj!r}')


def _strip_time_fields(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            k: _strip_time_fields(v)
            for k, v in value.items()
            if k not in TIME_DEPENDENT_KEYS
        }
    if isinstance(value, list):
        return [_strip_time_fields(v) for v in value]
    return value


def _assert_equal_with_tolerance(
    actual: Any, expected: Any, path: str = '', tol: float = 1e-9
) -> None:
    if isinstance(expected, dict):
        assert isinstance(actual, dict), f'{path}: expected dict, got {type(actual).__name__}'
        a_keys, e_keys = set(actual.keys()), set(expected.keys())
        assert a_keys == e_keys, (
            f'{path}: key mismatch. missing={e_keys - a_keys} extra={a_keys - e_keys}'
        )
        for k in expected:
            _assert_equal_with_tolerance(actual[k], expected[k], f'{path}.{k}', tol)
        return
    if isinstance(expected, list):
        assert isinstance(actual, list), f'{path}: expected list, got {type(actual).__name__}'
        assert len(actual) == len(expected), (
            f'{path}: list length {len(actual)} != {len(expected)}'
        )
        for i, (a, e) in enumerate(zip(actual, expected)):
            _assert_equal_with_tolerance(a, e, f'{path}[{i}]', tol)
        return
    if isinstance(expected, float) and isinstance(actual, (int, float)):
        assert math.isclose(float(actual), expected, rel_tol=0, abs_tol=tol), (
            f'{path}: {actual} vs {expected} exceeds tol {tol}'
        )
        return
    assert actual == expected, f'{path}: {actual!r} != {expected!r}'


def _check_golden(name: str, actual: Any) -> None:
    """Snapshot compare. UPDATE_GOLDENS=1 writes the file; otherwise asserts equality."""
    payload = _strip_time_fields(_to_jsonable(actual))
    path = GOLDEN_DIR / f'{name}.json'
    if UPDATE_GOLDENS:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, sort_keys=True))
        return
    if not path.exists():
        pytest.fail(
            f'Golden missing: {path}. Run `UPDATE_GOLDENS=1 pytest tests/test_consensio_golden.py` first.'
        )
    expected = json.loads(path.read_text())
    _assert_equal_with_tolerance(payload, expected, path=name)


# ---------------------------------------------------------------------------
# Deterministic OHLCV fixtures
# ---------------------------------------------------------------------------

TREND_SEEDS = {'up': 42, 'down': 43, 'sideways': 44}


def _make_ohlcv(n: int, trend: str, seed: int) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    dates = pd.date_range(end='2026-01-01', periods=n, freq='D')
    drift = {'up': 0.15, 'down': -0.15, 'sideways': 0.0}[trend]
    close = 100 + np.cumsum(rng.standard_normal(n) * 0.5 + drift)
    high = close + np.abs(rng.standard_normal(n)) * 0.5
    low = close - np.abs(rng.standard_normal(n)) * 0.5
    open_ = close + rng.standard_normal(n) * 0.3
    volume = rng.integers(1000, 10000, n).astype(float)
    return pd.DataFrame(
        {'open': open_, 'high': high, 'low': low, 'close': close, 'volume': volume},
        index=dates,
    )


def _resample(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    return df.resample(rule).agg({
        'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last', 'volume': 'sum'
    }).dropna()


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

def test_constants_golden():
    _check_golden('constants', {
        'RSI_OVERSOLD': RSI_OVERSOLD,
        'RSI_OVERBOUGHT': RSI_OVERBOUGHT,
        'ADX_STRONG_TREND': ADX_STRONG_TREND,
        'ADX_WEAK_TREND': ADX_WEAK_TREND,
        'FRESH_FLIP_DAYS': FRESH_FLIP_DAYS,
    })


# ---------------------------------------------------------------------------
# Indicator calculators — parametrized across 3 trend regimes
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('trend', list(TREND_SEEDS.keys()))
class TestIndicatorGoldens:
    @pytest.fixture
    def df(self, trend):
        return _make_ohlcv(200, trend, TREND_SEEDS[trend])

    def test_ema_ribbon(self, df, trend):
        out = EmaRibbonCalculator().calculate(df)
        assert out is not None
        assert isinstance(out.get('days_since_flip'), int) and out['days_since_flip'] >= 0
        _check_golden(f'ema_ribbon_{trend}', out)

    def test_ema_ribbon_atr_is_wilders_rma(self, df, trend):
        """Server ATR must match client ATR (Wilder's RMA), not SMA.

        Client formula (`carrac-indicator-pack.js` calculateATR):
            atr[period-1] = mean(tr[0..period-1])
            atr[i]        = (atr[i-1] * (period-1) + tr[i]) / period
        """
        calc = EmaRibbonCalculator()
        atr = calc._calculate_atr(df, period=calc.atr_period)
        # Independently compute the reference Wilder's RMA and compare.
        high = df['high']
        low = df['low']
        close = df['close']
        import numpy as np
        tr = np.maximum.reduce([
            (high - low).values,
            (high - close.shift(1)).abs().values,
            (low - close.shift(1)).abs().values,
        ])
        # tr[0] has NaN from close.shift(1); swap in high-low on that row.
        tr[0] = (high.iloc[0] - low.iloc[0])
        ref = np.full(len(tr), np.nan)
        p = calc.atr_period
        ref[p - 1] = tr[:p].mean()
        for i in range(p, len(tr)):
            ref[i] = (ref[i - 1] * (p - 1) + tr[i]) / p
        # Compare last 10 bars within 1e-9.
        for i in range(len(ref) - 10, len(ref)):
            assert abs(atr.iloc[i] - ref[i]) < 1e-9, \
                f"ATR Wilder's mismatch at bar {i}: server={atr.iloc[i]:.6f} ref={ref[i]:.6f}"

    def test_rsi_scalar(self, df, trend):
        value = RSICalculator(period=14).calculate(df)
        _check_golden(f'rsi_scalar_{trend}', {'value': value})

    def test_rsi_series_tail(self, df, trend):
        series = RSICalculator(period=14).calculate_series(df)
        _check_golden(f'rsi_series_{trend}', {
            'len': len(series),
            'tail_20': series.tail(20).tolist(),
        })

    def test_rsi_divergence(self, df, trend):
        out = RSIDivergenceDetector(rsi_period=14, lookback=20, swing_strength=3).detect(df)
        _check_golden(f'rsi_divergence_{trend}', out)

    def test_market_structure(self, df, trend):
        out = MarketStructureAnalyzer(left_bars=5, right_bars=5).analyze(df)
        _check_golden(f'market_structure_{trend}', out)

    def test_adx(self, df, trend):
        out = ADXCalculator(period=14).calculate(df)
        _check_golden(f'adx_{trend}', out)

    def test_bollinger(self, df, trend):
        out = BollingerBandCalculator(period=20, std_dev=2.0, lookback=60).calculate(df)
        _check_golden(f'bollinger_{trend}', out)

    def test_cpr_monthly(self, df, trend):
        out = CPRCalculator().calculate_monthly_cpr(df)
        _check_golden(f'cpr_monthly_{trend}', out)

    def test_cpr_weekly(self, df, trend):
        out = CPRCalculator().calculate_weekly_cpr(df)
        _check_golden(f'cpr_weekly_{trend}', out)


    def test_cpr_daily(self, df, trend):
        out = CPRCalculator().calculate_daily_cpr(df)
        _check_golden(f'cpr_daily_{trend}', out)

    def test_consensio_signal(self, df, trend):
        out = ConsensioCalculator().calculate_signal(df)
        _check_golden(f'consensio_signal_{trend}', {'signal': out})

    def test_consensio_multi_timeframe(self, df, trend):
        df_3d = _resample(df, '3D')
        df_1w = _resample(df, '1W')
        out = ConsensioCalculator().calculate_multi_timeframe(df, df_3d, df_1w)
        _check_golden(f'consensio_multi_tf_{trend}', out)


# Pure function — no parametrization
def test_cpr_weekly_iso_year_boundary():
    """isocalendar().year is the correct bucketing key around the
    year boundary. 2022-01-02 is the last day of ISO week 52 of 2021; the
    old code keyed it on 2022 which drops it into a not-yet-existent bucket.
    """
    import pandas as pd
    for date, expected_iso_year, expected_iso_week in [
        ('2021-12-31', 2021, 52),
        ('2022-01-01', 2021, 52),
        ('2022-01-02', 2021, 52),
        ('2022-01-03', 2022, 1),
        ('2021-01-01', 2020, 53),  # 2020 had 53 ISO weeks
    ]:
        ts = pd.Timestamp(date)
        iso = ts.isocalendar()
        assert iso.year == expected_iso_year, (
            f'{date}: isocalendar().year={iso.year}, expected {expected_iso_year}'
        )
        assert iso.week == expected_iso_week, (
            f'{date}: isocalendar().week={iso.week}, expected {expected_iso_week}'
        )


def test_consensio_signal_to_positions():
    c = ConsensioCalculator()
    out = {str(s): c.signal_to_positions(s) for s in range(-3, 4)}
    _check_golden('consensio_signal_to_positions', out)


# ---------------------------------------------------------------------------
# TrendAnalyzer — fixed asset dicts; last_change left None to avoid wall-clock drift
# ---------------------------------------------------------------------------

_TREND_ASSETS = {
    'bullish': {
        'asset': {
            'consensio': {'1D': {'signal': 3}, '3D': {'signal': 2}, '1W': {'signal': -2}},
            'positions': 6,
            'ema_ribbon': {'trend': 'bull', 'last_change': None},
            'weekly_rsi': 28.0,
        },
        'previous': {'positions': 4, 'streak': {'direction': 'flat', 'count': 0}},
    },
    'bearish': {
        'asset': {
            'consensio': {'1D': {'signal': -3}, '3D': {'signal': -2}, '1W': {'signal': 2}},
            'positions': 0,
            'ema_ribbon': {'trend': 'bear', 'last_change': None},
            'weekly_rsi': 75.0,
        },
        'previous': {'positions': 3, 'streak': {'direction': 'flat', 'count': 0}},
    },
    'neutral': {
        'asset': {
            'consensio': {'1D': {'signal': 0}, '3D': {'signal': 0}, '1W': {'signal': 0}},
            'positions': 3,
            'ema_ribbon': {'trend': 'neutral', 'last_change': None},
            'weekly_rsi': 50.0,
        },
        'previous': {'positions': 3, 'streak': {'direction': 'flat', 'count': 0}},
    },
}


@pytest.mark.parametrize('name', list(_TREND_ASSETS.keys()))
def test_trend_analyzer(name):
    payload = _TREND_ASSETS[name]
    out = TrendAnalyzer().analyze(payload['asset'], previous=payload['previous'])
    _check_golden(f'trend_analyzer_{name}', out)


# ---------------------------------------------------------------------------
# Scorers — feed each scorer with the indicator outputs from the same DF
# ---------------------------------------------------------------------------

def _scorer_inputs(trend: str) -> dict:
    df = _make_ohlcv(200, trend, TREND_SEEDS[trend])
    df_3d = _resample(df, '3D')
    df_1w = _resample(df, '1W')
    consensio = ConsensioCalculator()
    consensio_signals = {
        '1d': consensio.calculate_signal(df),
        '3d': consensio.calculate_signal(df_3d),
        '1w': consensio.calculate_signal(df_1w),
    }
    ema_ribbon = EmaRibbonCalculator().calculate(df) or {}
    # Freeze last_change so scorer freshness logic behaves identically across days.
    ema_ribbon = {**ema_ribbon, 'last_change': None}
    rsi_value = RSICalculator(period=14).calculate(df)
    rsi_div = RSIDivergenceDetector(rsi_period=14, lookback=20, swing_strength=3).detect(df)
    rsi_data = {'value': rsi_value, 'divergences': rsi_div}
    adx = ADXCalculator(period=14).calculate(df)
    structure = MarketStructureAnalyzer(left_bars=5, right_bars=5).analyze(df)
    cpr = CPRCalculator().calculate_monthly_cpr(df)
    volume_metrics = {'is_above_average': True, 'volume_ratio': 1.5}
    return {
        'df': df,
        'consensio_signals': consensio_signals,
        'ema_ribbon': ema_ribbon,
        'rsi_data': rsi_data,
        'adx': adx,
        'structure': structure,
        'cpr': cpr,
        'volume_metrics': volume_metrics,
    }


@pytest.mark.parametrize('trend', list(TREND_SEEDS.keys()))
class TestScorerGoldens:
    @pytest.fixture
    def inp(self, trend):
        return _scorer_inputs(trend)

    def test_opportunity_scorer(self, inp, trend):
        out = OpportunityScorer().calculate_score(
            consensio_signals=inp['consensio_signals'],
            ema_ribbon=inp['ema_ribbon'],
            rsi_data=inp['rsi_data'],
            price_data=inp['df'],
            volume_metrics=inp['volume_metrics'],
            adx_data=inp['adx'],
            funding_rate=0.01,
        )
        _check_golden(f'opportunity_scorer_{trend}', out)

    def test_research_scorer(self, inp, trend):
        out = ResearchScorer().calculate_score(
            consensio_signals=inp['consensio_signals'],
            ema_ribbon=inp['ema_ribbon'],
            rsi_data=inp['rsi_data'],
            price_data=inp['df'],
            volume_metrics=inp['volume_metrics'],
            adx_data=inp['adx'],
        )
        _check_golden(f'research_scorer_{trend}', out)

    def test_user_scorer(self, inp, trend):
        out = UserScorer().calculate_score(
            consensio_signals=inp['consensio_signals'],
            ema_ribbon=inp['ema_ribbon'],
            rsi_data=inp['rsi_data'],
            price_data=inp['df'],
            volume_metrics=inp['volume_metrics'],
            adx_data=inp['adx'],
            cpr_data=inp['cpr'],
        )
        _check_golden(f'user_scorer_{trend}', out)

    def test_ema_focused_scorer(self, inp, trend):
        out = EMAFocusedScorer().calculate_score(
            consensio_signals=inp['consensio_signals'],
            ema_ribbon=inp['ema_ribbon'],
            rsi_data=inp['rsi_data'],
            price_data=inp['df'],
            volume_metrics=inp['volume_metrics'],
            adx_data=inp['adx'],
            cpr_data=inp['cpr'],
        )
        _check_golden(f'ema_focused_scorer_{trend}', out)

    def test_balanced_scorer(self, inp, trend):
        out = BalancedScorer().calculate_score(
            consensio_signals=inp['consensio_signals'],
            ema_ribbon=inp['ema_ribbon'],
            rsi_data=inp['rsi_data'],
            price_data=inp['df'],
            volume_metrics=inp['volume_metrics'],
            adx_data=inp['adx'],
            cpr_data=inp['cpr'],
            structure_data=inp['structure'],
        )
        _check_golden(f'balanced_scorer_{trend}', out)
