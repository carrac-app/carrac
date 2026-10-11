"""
Quant Engine test suite.

Unit tests for pure functions (no DB required):
- Config validation
- Condition parsing
- Z-score / percentile computation
- Forward return calculation
- Analogue delta computation
- Realized volatility math
- Correlation stability math

Integration tests (marked @pytest.mark.integration, require production DB):
- API endpoint smoke tests
- Data freshness checks
- Engine output validation
"""

import math
import random
import statistics
from datetime import date, timedelta
from unittest.mock import patch, MagicMock

import pytest

try:
    import numpy as np
    import pandas as pd  # noqa: F401 (availability probe)
    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False

needs_numpy = pytest.mark.skipif(not HAS_NUMPY, reason='numpy/pandas not installed')


class _DictSeries:
    """Minimal pd.Series stand-in for forward return tests."""
    def __init__(self, data):
        self._data = data
        self.index = list(data.keys())
    def __getitem__(self, key):
        return self._data[key]


def _spearman_r(x, y):
    """Pure-Python Spearman rank correlation."""
    def _rank(data):
        indexed = sorted(range(len(data)), key=lambda i: data[i])
        ranks = [0.0] * len(data)
        i = 0
        while i < len(indexed):
            j = i
            while j < len(indexed) - 1 and data[indexed[j + 1]] == data[indexed[j]]:
                j += 1
            avg = (i + j) / 2 + 1
            for k in range(i, j + 1):
                ranks[indexed[k]] = avg
            i = j + 1
        return ranks
    rx, ry = _rank(list(x)), _rank(list(y))
    n = len(rx)
    mx, my = sum(rx) / n, sum(ry) / n
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    sx = math.sqrt(sum((a - mx) ** 2 for a in rx))
    sy = math.sqrt(sum((b - my) ** 2 for b in ry))
    return cov / (sx * sy) if sx and sy else 0.0


# ─── Config validation ──────────────────────────────────────────────

class TestConfig:
    def test_canonical_metrics_valid(self):
        from quant.engine.config import CANONICAL_METRICS
        assert len(CANONICAL_METRICS) >= 45  # 34 original + 8 chain + 3 factors
        valid_tables = {'price_daily', 'macro', 'derivatives', 'sentiment', 'onchain', 'signals'}
        valid_transforms = {'log_return', 'first_diff', 'raw'}
        valid_categories = {'price', 'macro', 'derivatives', 'sentiment', 'signals'}
        for name, cfg in CANONICAL_METRICS.items():
            assert cfg['table'] in valid_tables, f"{name}: invalid table {cfg['table']}"
            assert cfg['transform'] in valid_transforms, f"{name}: invalid transform"
            assert cfg['category'] in valid_categories, f"{name}: invalid category"

    def test_leadlag_pairs_reference_valid_metrics(self):
        from quant.engine.config import CANONICAL_METRICS, LEADLAG_PAIRS
        for leader, target, lag_range, desc in LEADLAG_PAIRS:
            assert leader in CANONICAL_METRICS, f"Leader {leader} not in CANONICAL_METRICS"
            assert target in CANONICAL_METRICS, f"Target {target} not in CANONICAL_METRICS"
            assert lag_range[0] < lag_range[1], f"Invalid lag range for {leader}->{target}"

    def test_analogue_metrics_exist_in_zscores(self):
        from quant.engine.config import ANALOGUE_METRICS, CANONICAL_METRICS, ZSCORE_ALIASES
        # Each analogue metric should be either a canonical metric name or a zscore alias value
        canonical_keys = set(CANONICAL_METRICS.keys())
        alias_values = set(ZSCORE_ALIASES.values())
        zscore_metrics = set()
        for name, cfg in CANONICAL_METRICS.items():
            zscore_metrics.add(cfg.get('metric', name))
        for m in ANALOGUE_METRICS:
            assert m in canonical_keys or m in alias_values or m in zscore_metrics, \
                f"Analogue metric {m} not traceable to canonical config"

    def test_correlation_windows_positive(self):
        from quant.engine.config import CORRELATION_WINDOWS, MIN_OBSERVATIONS
        for w in CORRELATION_WINDOWS:
            assert w > MIN_OBSERVATIONS, f"Window {w} must be > MIN_OBSERVATIONS"

    def test_regime_features_valid(self):
        from quant.engine.config import CANONICAL_METRICS, REGIME_FEATURES
        for feat in REGIME_FEATURES:
            assert feat in CANONICAL_METRICS, f"Regime feature {feat} not in CANONICAL_METRICS"

    def test_target_assets_valid(self):
        from quant.engine.config import TARGET_ASSETS
        for name, (asset, source) in TARGET_ASSETS.items():
            assert isinstance(asset, str) and len(asset) > 0
            assert source in ('binance', 'yahoo')

    def test_zscore_aliases_map_to_real_metrics(self):
        from quant.engine.config import ZSCORE_ALIASES, CANONICAL_METRICS
        for canonical, alias in ZSCORE_ALIASES.items():
            assert canonical in CANONICAL_METRICS, f"Alias key {canonical} not in CANONICAL_METRICS"

    def test_composite_factors_reference_valid_components(self):
        from quant.engine.config import COMPOSITE_FACTORS, CANONICAL_METRICS
        for fname, fdef in COMPOSITE_FACTORS.items():
            assert 'components' in fdef, f"Factor {fname} missing components"
            assert 'flip_sign_ref' in fdef, f"Factor {fname} missing flip_sign_ref"
            assert fdef['flip_sign_ref'] in fdef['components'], \
                f"Factor {fname} flip_sign_ref {fdef['flip_sign_ref']} not in components"
            for comp in fdef['components']:
                assert comp in CANONICAL_METRICS, \
                    f"Factor {fname} component {comp} not in CANONICAL_METRICS"

    def test_transmission_chains_reference_valid_pairs(self):
        from quant.engine.config import TRANSMISSION_CHAINS, LEADLAG_PAIRS
        ll_set = set((l, t) for l, t, _, _ in LEADLAG_PAIRS)
        for cname, cdef in TRANSMISSION_CHAINS.items():
            assert 'links' in cdef, f"Chain {cname} missing links"
            assert 'description' in cdef, f"Chain {cname} missing description"
            for a, b in cdef['links']:
                assert (a, b) in ll_set, \
                    f"Chain {cname} link ({a}, {b}) not in LEADLAG_PAIRS"

    def test_factor_canonical_metrics_exist(self):
        from quant.engine.config import CANONICAL_METRICS
        for factor in ['RISK_APPETITE', 'LIQUIDITY', 'CREDIT_STRESS']:
            assert factor in CANONICAL_METRICS, f"Factor {factor} not in CANONICAL_METRICS"
            cfg = CANONICAL_METRICS[factor]
            assert cfg['table'] == 'sentiment'
            assert cfg['transform'] == 'raw'


try:
    import pandas as _pd  # noqa: F401 (availability probe)
    _HAS_PANDAS = True
except ImportError:
    _HAS_PANDAS = False

@pytest.mark.skipif(not _HAS_PANDAS, reason='pandas not installed')
class TestPCAFactor:
    """Test PCA factor computation logic."""

    def test_pca_basic(self):
        import numpy as np
        import pandas as pd
        from quant.engine.factors import _compute_pca_factor

        # Two perfectly correlated series → PC1 should explain ~100%
        dates = pd.date_range('2020-01-01', periods=200, freq='D')
        x = np.random.randn(200)
        series = {
            'A': pd.Series(x, index=dates),
            'B': pd.Series(x * 0.8 + np.random.randn(200) * 0.1, index=dates),
        }
        factor, explained = _compute_pca_factor(series, ['A', 'B'], 'A')
        assert factor is not None
        assert len(factor) == 200
        assert explained > 0.8  # Should explain most variance

    def test_pca_sign_convention(self):
        import numpy as np
        import pandas as pd
        from quant.engine.factors import _compute_pca_factor

        dates = pd.date_range('2020-01-01', periods=200, freq='D')
        x = np.random.randn(200)
        series = {
            'A': pd.Series(x, index=dates),
            'B': pd.Series(-x + np.random.randn(200) * 0.1, index=dates),
        }
        # With flip_ref='A', the factor should correlate positively with A
        factor, _ = _compute_pca_factor(series, ['A', 'B'], 'A')
        assert factor is not None
        corr = np.corrcoef(factor.values, series['A'].values)[0, 1]
        assert corr > 0, "Factor should correlate positively with flip_ref"

    def test_pca_insufficient_data(self):
        import pandas as pd
        from quant.engine.factors import _compute_pca_factor

        dates = pd.date_range('2020-01-01', periods=10, freq='D')
        series = {
            'A': pd.Series(range(10), index=dates, dtype=float),
        }
        factor, explained = _compute_pca_factor(series, ['A', 'B'], 'A')
        assert factor is None  # Only 1 component available, need 2

    def test_pca_constant_series(self):
        import numpy as np
        import pandas as pd
        from quant.engine.factors import _compute_pca_factor

        dates = pd.date_range('2020-01-01', periods=100, freq='D')
        series = {
            'A': pd.Series(np.ones(100), index=dates),
            'B': pd.Series(np.random.randn(100), index=dates),
        }
        factor, _ = _compute_pca_factor(series, ['A', 'B'], 'A')
        assert factor is not None  # Should handle constant column gracefully


@pytest.mark.skipif(not _HAS_PANDAS, reason='pandas not installed')
class TestWeeklyResampling:
    """Test weekly resampling in lead-lag engine."""

    def test_resample_weekly(self):
        import numpy as np
        import pandas as pd
        from quant.engine.leadlag import _resample_weekly

        # 250 days ≈ 35+ weeks, safely above MIN_OBSERVATIONS (30)
        dates = pd.date_range('2024-01-01', periods=250, freq='D')
        a = np.random.randn(250)
        b = np.random.randn(250)
        wa, wb, widx = _resample_weekly(a, b, dates)
        assert wa is not None
        assert len(wa) == len(wb)
        assert len(wa) < 250  # Should be ~35 weeks
        assert len(wa) >= 30
        # Verify index is Friday-aligned
        for d in widx:
            assert d.weekday() == 4, f"{d} is not a Friday"

    def test_resample_insufficient(self):
        import numpy as np
        import pandas as pd
        from quant.engine.leadlag import _resample_weekly

        dates = pd.date_range('2024-01-01', periods=20, freq='D')
        a = np.random.randn(20)
        b = np.random.randn(20)
        wa, wb, widx = _resample_weekly(a, b, dates)
        assert wa is None  # Less than MIN_OBSERVATIONS weeks


# ─── Condition parsing ──────────────────────────────────────────────

class TestConditionParsing:
    """Test condition parsing logic (reimplemented to avoid pandas import)."""

    @staticmethod
    def _parse(cond_str):
        parts = cond_str.split(':')
        if len(parts) != 3:
            raise ValueError(f"Invalid condition format: {cond_str}")
        op_map = {'gt': '>', 'lt': '<', 'gte': '>=', 'lte': '<='}
        if parts[1] not in op_map:
            raise ValueError(f"Unknown operator: {parts[1]}")
        return parts[0], op_map[parts[1]], float(parts[2])

    @staticmethod
    def _check(zscore, op, threshold):
        if op == '>':  return zscore > threshold
        if op == '<':  return zscore < threshold
        if op == '>=': return zscore >= threshold
        if op == '<=': return zscore <= threshold
        return False

    def test_parse_valid_conditions(self):
        assert self._parse('VIX:gt:2') == ('VIX', '>', 2.0)
        assert self._parse('M2:lt:-1.5') == ('M2', '<', -1.5)
        assert self._parse('HY_SPREAD:gte:1') == ('HY_SPREAD', '>=', 1.0)
        assert self._parse('FEAR_GREED:lte:0') == ('FEAR_GREED', '<=', 0.0)

    def test_parse_invalid_format(self):
        with pytest.raises(ValueError, match="Invalid condition format"):
            self._parse('VIX:gt')
        with pytest.raises(ValueError, match="Invalid condition format"):
            self._parse('VIX')

    def test_parse_invalid_operator(self):
        with pytest.raises(ValueError, match="Unknown operator"):
            self._parse('VIX:eq:2')

    def test_parse_invalid_threshold(self):
        with pytest.raises(ValueError):
            self._parse('VIX:gt:abc')

    def test_check_condition(self):
        assert self._check(2.5, '>', 2.0) is True
        assert self._check(1.5, '>', 2.0) is False
        assert self._check(-1.5, '<', -1.0) is True
        assert self._check(2.0, '>=', 2.0) is True
        assert self._check(1.99, '>=', 2.0) is False
        assert self._check(2.0, '<=', 2.0) is True


# ─── Forward return calculation ──────────────────────────────────────

class TestForwardReturn:
    """Tests for forward return calculation (mirrors quant.engine.conditional._forward_return)."""

    @staticmethod
    def _forward_return(prices, start_date, horizon):
        """Pure-Python replica of _forward_return (avoids pandas import)."""
        start_price = None
        for offset in range(4):
            d = start_date + timedelta(days=offset)
            if d in prices.index:
                start_price = prices[d]
                break
        if start_price is None or start_price <= 0:
            return None
        end_date = start_date + timedelta(days=horizon)
        end_price = None
        for offset in range(4):
            d = end_date + timedelta(days=offset)
            if d in prices.index:
                end_price = prices[d]
                break
        if end_price is None:
            return None
        return (end_price / start_price) - 1

    def test_simple_return(self):
        prices = _DictSeries({
            date(2024, 1, 1): 100.0,
            date(2024, 1, 2): 105.0,
            date(2024, 1, 31): 110.0,
        })
        ret = self._forward_return(prices, date(2024, 1, 1), 30)
        assert ret == pytest.approx(0.10, abs=0.01)

    def test_weekend_skip(self):
        """Forward return should find closest price within 3 days."""
        prices = _DictSeries({
            date(2024, 1, 1): 100.0,
            date(2024, 1, 4): 105.0,
            date(2024, 2, 1): 110.0,
        })
        ret = self._forward_return(prices, date(2024, 1, 2), 30)
        assert ret is not None

    def test_missing_returns_none(self):
        prices = _DictSeries({date(2024, 1, 1): 100.0})
        ret = self._forward_return(prices, date(2024, 1, 1), 30)
        assert ret is None


# ─── Z-score and percentile computation ─────────────────────────────

class TestZScoreComputation:
    def test_zscore_basic(self):
        """Z-score of a value at the mean should be 0."""
        values = [10, 20, 30, 40, 50]
        mean = sum(values) / len(values)  # 30
        variance = sum((v - mean) ** 2 for v in values) / len(values)
        std = math.sqrt(variance)
        zscore = (mean - mean) / std
        assert zscore == 0.0

    def test_zscore_positive(self):
        values = [10, 20, 30, 40, 50]
        mean = sum(values) / len(values)
        variance = sum((v - mean) ** 2 for v in values) / len(values)
        std = math.sqrt(variance)
        zscore = (50 - mean) / std
        assert zscore > 0

    def test_percentile_rank(self):
        """Percentile rank: % of values <= current value."""
        window_vals = [10, 20, 30, 40, 50]
        current = 40
        rank_below = sum(1 for v in window_vals if v <= current)
        percentile = (rank_below / len(window_vals)) * 100
        assert percentile == 80.0  # 4/5 = 80%

    def test_percentile_at_min(self):
        window_vals = [10, 20, 30, 40, 50]
        rank_below = sum(1 for v in window_vals if v <= 10)
        percentile = (rank_below / len(window_vals)) * 100
        assert percentile == 20.0  # 1/5 = 20%

    def test_percentile_at_max(self):
        window_vals = [10, 20, 30, 40, 50]
        rank_below = sum(1 for v in window_vals if v <= 50)
        percentile = (rank_below / len(window_vals)) * 100
        assert percentile == 100.0


# ─── Realized volatility (Parkinson) ────────────────────────────────

class TestRealizedVolatility:
    def test_parkinson_formula(self):
        """Parkinson vol from high/low range."""
        high, low = 105.0, 95.0
        hl_sq = math.log(high / low) ** 2
        # Single-day Parkinson: sqrt(hl^2 / (4*ln(2)))
        vol_daily = math.sqrt(hl_sq / (4 * math.log(2)))
        assert vol_daily > 0
        # Annualized
        vol_annual = vol_daily * math.sqrt(252) * 100
        assert vol_annual > 0 and vol_annual < 200  # Reasonable range

    def test_parkinson_zero_range(self):
        """If high == low, range is zero."""
        high, low = 100.0, 100.0
        # high == low → log(1) = 0 → vol = 0
        hl_sq = math.log(high / low) ** 2
        assert hl_sq == 0.0

    def test_parkinson_window(self):
        """30-day window computation."""
        # Simulate 30 days of data
        np.random.seed(42)
        hl_sq = [(math.log(1 + np.random.uniform(0.01, 0.05))) ** 2 for _ in range(30)]
        parkinson = math.sqrt(sum(hl_sq) / len(hl_sq) / (4 * math.log(2))) * math.sqrt(252) * 100
        assert 10 < parkinson < 100  # BTC-like vol range


# ─── Correlation stability ──────────────────────────────────────────

@needs_numpy
class TestFDRAndFisherCI:
    def test_fisher_ci_wraps_point_estimate(self):
        from quant.engine.correlations import _fisher_z_ci
        r, n = 0.6, 120
        lo, hi = _fisher_z_ci(r, n)
        assert lo is not None and hi is not None
        assert lo < r < hi
        # 95% CI on r=0.6 with n=120 should span roughly ±0.10
        assert 0.05 < (hi - lo) < 0.30

    def test_fisher_ci_degenerate(self):
        from quant.engine.correlations import _fisher_z_ci
        # n too small
        assert _fisher_z_ci(0.5, 3) == (None, None)
        # r=1
        assert _fisher_z_ci(1.0, 100) == (1.0, 1.0)
        # n=None
        assert _fisher_z_ci(0.5, None) == (None, None)

    def test_bh_fdr_basic(self):
        from quant.engine.correlations import _augment_batch
        # Four tests in same (ts, method, window) bucket with varied p-values.
        # 10th field = n_eff (None → falls back to n_obs for CI computation).
        rows = [
            ('2026-04-18', 'A', 'B', 60, 0.8,  0.001, 60, 'spearman', None, None),
            ('2026-04-18', 'A', 'C', 60, 0.5,  0.02,  60, 'spearman', None, None),
            ('2026-04-18', 'A', 'D', 60, 0.3,  0.1,   60, 'spearman', None, None),
            ('2026-04-18', 'A', 'E', 60, 0.1,  0.9,   60, 'spearman', None, None),
        ]
        out = _augment_batch(rows, apply_fdr=True)
        # q-values are monotone non-decreasing when sorted by p
        q_by_p = sorted([(r[5], r[9]) for r in out if r[9] is not None])
        qs = [q for _, q in q_by_p]
        assert qs == sorted(qs), 'q-values should be monotone with p'
        # BH is never less conservative than raw p
        for r in out:
            p, q = r[5], r[9]
            assert q >= p - 1e-9

    def test_significance_flag(self):
        from quant.engine.correlations import _augment_batch
        rows = [
            # Strong, big n, low p → significant (n_eff=None → uses nominal n=60)
            ('2026-04-18', 'A', 'B', 60, 0.7,  0.001, 60, 'spearman', None, None),
            # Huge p → not significant
            ('2026-04-18', 'A', 'C', 60, 0.05, 0.9,   60, 'spearman', None, None),
            # Small n, big r → not significant (practical threshold)
            ('2026-04-18', 'A', 'D', 60, 0.9,  0.04,  20, 'spearman', None, None),
        ]
        out = _augment_batch(rows, apply_fdr=True)
        assert out[0][12] is True
        assert out[1][12] is False
        assert out[2][12] is False


class TestCorrelationStability:
    def test_stable_correlation(self):
        """Perfectly correlated series should have stability near 0."""
        random.seed(42)
        n = 60
        x = [float(i) for i in range(n)]
        y = [v * 2 + random.gauss(0, 0.1) for v in x]

        half = n // 2
        sub_rs = []
        step = max(1, (n - half) // 4)
        for j in range(0, n - half + 1, step):
            rs = _spearman_r(x[j:j + half], y[j:j + half])
            sub_rs.append(rs)
        stability = statistics.pstdev(sub_rs)
        assert stability < 0.05  # Very stable

    def test_unstable_correlation(self):
        """Series that flip correlation should have high instability."""
        n = 60
        x = [float(i) for i in range(n)]
        y = [v * 2 for v in x[:30]] + [-v * 2 for v in x[30:]]

        half = n // 2
        sub_rs = []
        step = max(1, (n - half) // 4)
        for j in range(0, n - half + 1, step):
            rs = _spearman_r(x[j:j + half], y[j:j + half])
            sub_rs.append(rs)
        stability = statistics.pstdev(sub_rs)
        assert stability > 0.3  # Unstable


# ─── Analogue momentum (delta z-scores) ─────────────────────────────

class TestAnalogueMomentum:
    def test_delta_computation(self):
        """7-day delta z-scores should be computed correctly."""
        from collections import defaultdict
        date_vectors = defaultdict(dict)
        # Simulate 10 days of z-scores
        for i in range(10):
            d = date(2024, 1, 1) + timedelta(days=i)
            date_vectors[d]['VIX'] = float(i)  # VIX z-score increases linearly

        # Compute deltas (same logic as conditional.py)
        dates_sorted = sorted(date_vectors.keys())
        for i, d in enumerate(dates_sorted):
            if i < 7:
                continue
            d_prev = dates_sorted[i - 7]
            for metric in list(date_vectors[d].keys()):
                if metric in date_vectors[d_prev]:
                    delta = date_vectors[d][metric] - date_vectors[d_prev][metric]
                    date_vectors[d][f'{metric}_D7'] = delta

        # Day 7: VIX=7, VIX 7 days ago = 0 → delta = 7
        d7 = date(2024, 1, 8)
        assert date_vectors[d7]['VIX_D7'] == 7.0
        # Day 8: VIX=8, VIX 7 days ago = 1 → delta = 7
        d8 = date(2024, 1, 9)
        assert date_vectors[d8]['VIX_D7'] == 7.0

    def test_delta_not_computed_for_early_dates(self):
        from collections import defaultdict
        date_vectors = defaultdict(dict)
        for i in range(5):
            d = date(2024, 1, 1) + timedelta(days=i)
            date_vectors[d]['VIX'] = float(i)

        dates_sorted = sorted(date_vectors.keys())
        for i, d in enumerate(dates_sorted):
            if i < 7:
                continue
            # Should not reach here with only 5 days
            date_vectors[d]['VIX_D7'] = 0

        # No delta keys should exist
        for d in date_vectors:
            assert 'VIX_D7' not in date_vectors[d]


# ─── Analogue similarity basis disclosure ──────────────────────────────

class _AnalogueDBCursor:
    """Routes SELECT queries to per-shape return values based on
    keywords in the SQL text, mirroring the pattern used for
    carrac.db_pool.get_quant_conn mocks elsewhere in the suite."""

    def __init__(self, zscore_rows, price_rows=None):
        self.zscore_rows = zscore_rows
        self.price_rows = price_rows or []
        self._last_sql = ''

    def execute(self, sql, params=None):
        self._last_sql = sql or ''

    def fetchall(self):
        if 'quant.zscores' in self._last_sql:
            return self.zscore_rows
        if 'quant.price_daily' in self._last_sql:
            return self.price_rows
        return []


def _analogue_conn_ctx(cursor):
    """Patch target for quant.engine.conditional.get_conn (imported at
    module level there, so carrac.db_pool.get_quant_conn can't be patched after the
    fact -- the name must be patched where it's bound)."""
    from contextlib import contextmanager

    @contextmanager
    def _ctx():
        conn = MagicMock()
        conn.cursor.return_value = cursor
        yield conn
    return _ctx


class TestQueryAnaloguesSimilarityBasis:
    """query_analogues() computes each analogue's cosine similarity over
    only the metrics common to the reference date AND that historical
    date -- a different subset per analogue. The response must disclose
    this basis both top-level (similarity_basis) and per-entry (basis),
    additively, without touching any pre-existing field."""

    FAKE_METRICS = [f'M{i}' for i in range(1, 11)]  # 10 fake metrics

    def _rows(self, ts, metrics, zscore=1.0):
        return [{'ts': ts, 'metric': m, 'zscore': zscore} for m in metrics]

    def test_similarity_basis_top_level_present_with_correct_counts(self, monkeypatch):
        from quant.engine import conditional

        monkeypatch.setattr(conditional, 'ANALOGUE_METRICS', self.FAKE_METRICS)

        ref_date = date(2024, 6, 1)
        hist_date = date(2020, 1, 15)  # well over 90 days before ref

        zscore_rows = (
            self._rows(ref_date, self.FAKE_METRICS[:8])   # ref: 8/10 metrics
            + self._rows(hist_date, self.FAKE_METRICS[:6], zscore=0.5)  # hist: 6 overlap
        )
        cursor = _AnalogueDBCursor(zscore_rows)

        with patch.object(conditional, 'get_conn', _analogue_conn_ctx(cursor)):
            result = conditional.query_analogues(
                target='BTC', top_n=5, reference_date='2024-06-01',
            )

        assert 'similarity_basis' in result
        basis = result['similarity_basis']
        assert basis['analogue_metrics_total'] == 10
        assert basis['reference_metrics_available'] == 8
        assert basis['min_common_required'] == 5
        assert isinstance(basis['note'], str) and basis['note']

    def test_per_entry_basis_matches_n_common_metrics(self, monkeypatch):
        from quant.engine import conditional

        monkeypatch.setattr(conditional, 'ANALOGUE_METRICS', self.FAKE_METRICS)

        ref_date = date(2024, 6, 1)
        hist1 = date(2020, 1, 15)   # overlaps on 6/10 metrics
        hist2 = date(2019, 1, 1)    # overlaps on 8/10 metrics (all of ref's 8)

        zscore_rows = (
            self._rows(ref_date, self.FAKE_METRICS[:8])
            + self._rows(hist1, self.FAKE_METRICS[:6], zscore=0.5)
            + self._rows(hist2, self.FAKE_METRICS[:8], zscore=-0.3)
        )
        cursor = _AnalogueDBCursor(zscore_rows)

        with patch.object(conditional, 'get_conn', _analogue_conn_ctx(cursor)):
            result = conditional.query_analogues(
                target='BTC', top_n=5, reference_date='2024-06-01',
            )

        analogues = result['analogues']
        assert len(analogues) == 2
        for entry in analogues:
            expected = f"{entry['n_common_metrics']}/10"
            assert entry['basis'] == expected

        by_date = {a['date']: a for a in analogues}
        assert by_date[str(hist1)]['n_common_metrics'] == 6
        assert by_date[str(hist1)]['basis'] == '6/10'
        assert by_date[str(hist2)]['n_common_metrics'] == 8
        assert by_date[str(hist2)]['basis'] == '8/10'

    def test_existing_fields_unchanged(self, monkeypatch):
        """The fix is additive only -- every pre-existing top-level and
        per-entry field must still be present with its original meaning."""
        from quant.engine import conditional

        monkeypatch.setattr(conditional, 'ANALOGUE_METRICS', self.FAKE_METRICS)

        ref_date = date(2024, 6, 1)
        hist_date = date(2020, 1, 15)

        zscore_rows = (
            self._rows(ref_date, self.FAKE_METRICS[:8])
            + self._rows(hist_date, self.FAKE_METRICS[:6], zscore=0.5)
        )
        cursor = _AnalogueDBCursor(zscore_rows)

        with patch.object(conditional, 'get_conn', _analogue_conn_ctx(cursor)):
            result = conditional.query_analogues(
                target='BTC', top_n=5, reference_date='2024-06-01',
            )

        for key in ('reference_date', 'target', 'current_vector', 'n_metrics', 'analogues'):
            assert key in result
        assert result['reference_date'] == str(ref_date)
        assert result['target'] == 'BTC'
        assert result['n_metrics'] == 8

        entry = result['analogues'][0]
        for key in ('date', 'similarity', 'vector', 'forward_return', 'n_common_metrics'):
            assert key in entry
        assert entry['n_common_metrics'] == 6


# ─── Net liquidity derivation ────────────────────────────────────────

class TestNetLiquidity:
    def test_net_liquidity_formula(self):
        """NET_LIQUIDITY = FED_BALANCE - TGA - REVERSE_REPO"""
        fed = 7500000.0  # 7.5T
        tga = 750000.0   # 750B
        rrp = 500000.0   # 500B
        net_liq = fed - tga - rrp
        assert net_liq == 6250000.0

    def test_closest_on_or_before(self):
        """REVERSE_REPO (daily) should align to weekly FED_BALANCE dates."""
        # Simulate the lookup logic from normalize.py
        rrp = {
            date(2024, 1, 1): 500.0,
            date(2024, 1, 2): 510.0,
            date(2024, 1, 3): 520.0,
            date(2024, 1, 4): 530.0,
            date(2024, 1, 5): 540.0,
        }
        rrp_dates = sorted(rrp.keys())

        def _closest_on_or_before(target, sorted_dates, values):
            best = None
            for d in sorted_dates:
                if d <= target:
                    best = d
                else:
                    break
            return values[best] if best is not None else None

        # Weekly date Jan 3 should find Jan 3 RRP
        assert _closest_on_or_before(date(2024, 1, 3), rrp_dates, rrp) == 520.0
        # Date before any data should return None
        assert _closest_on_or_before(date(2023, 12, 31), rrp_dates, rrp) is None


# ─── Unconditional baseline ─────────────────────────────────────────

class TestUnconditionalBaseline:
    def test_baseline_stats_structure(self):
        """Baseline should have mean, median, std, hit_rate, n."""
        returns = [0.05, -0.03, 0.08, -0.01, 0.12, -0.06, 0.04]
        baseline = {
            'mean': round(statistics.mean(returns), 4),
            'median': round(statistics.median(returns), 4),
            'std': round(statistics.pstdev(returns), 4),
            'hit_rate': round(sum(1 for r in returns if r > 0) / len(returns), 3),
            'n': len(returns),
        }
        assert baseline['n'] == 7
        assert 0 <= baseline['hit_rate'] <= 1
        assert baseline['std'] > 0

    def test_edge_calculation(self):
        """Edge = conditional median - unconditional median."""
        conditional_median = 0.08
        unconditional_median = 0.03
        edge = conditional_median - unconditional_median
        assert edge == pytest.approx(0.05)


# ─── ETF flows parser ───────────────────────────────────────────────

class TestETFFlowsParser:
    def test_parse_number(self):
        from quant.collectors.etf_flows import ETFFlowsCollector
        parse = ETFFlowsCollector._parse_number
        assert parse('1,234.5') == 1234.5
        assert parse('(500.0)') == -500.0
        assert parse('-123') == -123.0
        assert parse('') is None
        assert parse('-') is None
        assert parse('N/A') is None
        assert parse('$1,000') == 1000.0

    def test_parse_date(self):
        from quant.collectors.etf_flows import ETFFlowsCollector
        parse = ETFFlowsCollector._parse_date
        d = parse('15 Jan 2024')
        assert d is not None
        assert d.year == 2024 and d.month == 1 and d.day == 15
        assert parse('invalid') is None


# ─── Event calendar ──────────────────────────────────────────────────

class TestEventCalendar:
    def test_all_events_have_required_fields(self):
        from quant.collectors.event_calendar import SCHEDULED_EVENTS
        for evt in SCHEDULED_EVENTS:
            assert 'ts' in evt, "Missing ts in event"
            assert 'event_type' in evt, f"Missing event_type in {evt}"
            assert 'title' in evt, f"Missing title in {evt}"
            assert 'impact' in evt, f"Missing impact in {evt}"

    def test_events_are_in_known_years(self):
        """All events must fall within a known scheduled year (2026 or later)."""
        from quant.collectors.event_calendar import SCHEDULED_EVENTS
        for evt in SCHEDULED_EVENTS:
            year = int(evt['ts'][:4])
            assert year >= 2026, f"Event predates 2026: {evt}"

    def test_fomc_count_2026(self):
        """FOMC should have 8 meetings in 2026."""
        from quant.collectors.event_calendar import SCHEDULED_EVENTS
        fomc = [e for e in SCHEDULED_EVENTS if e['event_type'] == 'FOMC' and e['ts'].startswith('2026-')]
        assert len(fomc) == 8

    def test_fomc_count_2027(self):
        """FOMC should have 8 meetings in 2027."""
        from quant.collectors.event_calendar import SCHEDULED_EVENTS
        fomc = [e for e in SCHEDULED_EVENTS if e['event_type'] == 'FOMC' and e['ts'].startswith('2027-')]
        assert len(fomc) == 8

    def test_cpi_count(self):
        """CPI should have 12 releases in 2026 (2027 not yet published by BLS)."""
        from quant.collectors.event_calendar import SCHEDULED_EVENTS
        cpi = [e for e in SCHEDULED_EVENTS if e['event_type'] == 'CPI' and e['ts'].startswith('2026-')]
        assert len(cpi) == 12

    def test_nfp_count(self):
        """NFP should have 12 releases in 2026 (2027 not yet published by BLS)."""
        from quant.collectors.event_calendar import SCHEDULED_EVENTS
        nfp = [e for e in SCHEDULED_EVENTS if e['event_type'] == 'NFP' and e['ts'].startswith('2026-')]
        assert len(nfp) == 12

    def test_no_duplicate_events(self):
        from quant.collectors.event_calendar import SCHEDULED_EVENTS
        keys = [(e['ts'], e['event_type'], e['title']) for e in SCHEDULED_EVENTS]
        assert len(keys) == len(set(keys)), "Duplicate events found"


# ─── Cross-sectional dispersion ────────────────────────────────────

class TestCrossDispersion:
    def test_dispersion_basic(self):
        """Dispersion = population std of 30d returns across altcoins."""
        returns = [0.10, -0.05, 0.08, 0.02, -0.03]
        mean_ret = sum(returns) / len(returns)
        variance = sum((r - mean_ret) ** 2 for r in returns) / len(returns)
        std_ret = math.sqrt(variance)
        assert std_ret > 0
        assert std_ret < 1  # Reasonable range

    def test_dispersion_identical_returns(self):
        """All altcoins moving together = zero dispersion."""
        returns = [0.05, 0.05, 0.05, 0.05, 0.05]
        mean_ret = sum(returns) / len(returns)
        variance = sum((r - mean_ret) ** 2 for r in returns) / len(returns)
        std_ret = math.sqrt(variance)
        assert std_ret == 0.0

    def test_dispersion_in_config(self):
        """CROSS_DISPERSION should be in CANONICAL_METRICS."""
        from quant.engine.config import CANONICAL_METRICS
        assert 'CROSS_DISPERSION' in CANONICAL_METRICS
        cfg = CANONICAL_METRICS['CROSS_DISPERSION']
        assert cfg['table'] == 'sentiment'
        assert cfg['metric'] == 'CROSS_DISPERSION'
        assert cfg['transform'] == 'raw'


# ─── Robust z-score (median/MAD) ──────────────────────────────────

class TestRobustZScore:
    """Tests for median/MAD z-score computation matching normalize.py."""

    @staticmethod
    def _compute(window_vals, current_value):
        """Replicate the exact algorithm from normalize.py."""
        sorted_w = sorted(window_vals)
        n = len(sorted_w)
        median = (sorted_w[n // 2] + sorted_w[(n - 1) // 2]) / 2
        abs_devs = sorted(abs(v - median) for v in window_vals)
        mad = (abs_devs[n // 2] + abs_devs[(n - 1) // 2]) / 2
        scaled_mad = mad * 1.4826
        if scaled_mad == 0:
            return None, median, scaled_mad
        zscore = (current_value - median) / scaled_mad
        return zscore, median, scaled_mad

    def test_median_odd_list(self):
        z, median, _ = self._compute([1, 3, 5, 7, 9], 5)
        assert median == 5.0

    def test_median_even_list(self):
        z, median, _ = self._compute([1, 3, 5, 7], 4)
        assert median == 4.0  # (3+5)/2

    def test_mad_scaling_factor(self):
        vals = [1, 2, 3, 4, 5]
        _, median, scaled_mad = self._compute(vals, 3)
        assert median == 3.0
        # MAD of [1,2,3,4,5] around 3: deviations = [0,1,1,2,2], median dev = 1
        assert abs(scaled_mad - 1.4826) < 0.001

    def test_zscore_at_median_is_zero(self):
        z, median, _ = self._compute([10, 20, 30, 40, 50], 30)
        assert z == 0.0

    def test_zscore_positive(self):
        z, _, _ = self._compute([10, 20, 30, 40, 50], 60)
        assert z > 0

    def test_zscore_negative(self):
        z, _, _ = self._compute([10, 20, 30, 40, 50], 0)
        assert z < 0

    def test_constant_series_skipped(self):
        z, _, scaled_mad = self._compute([5, 5, 5, 5, 5], 5)
        assert scaled_mad == 0.0
        assert z is None

    def test_fat_tail_outlier_detection(self):
        """Robust z-score should be higher than mean/std for genuine outliers."""
        # Core data with natural variation + one extreme outlier
        vals = list(range(8, 38)) + [500]  # 30 values 8-37, plus 500
        # Robust (outlier doesn't distort median/MAD of the core)
        robust_z, _, _ = self._compute(vals, 500)
        # Mean/std (outlier inflates both mean and std)
        mean = sum(vals) / len(vals)
        std = math.sqrt(sum((v - mean) ** 2 for v in vals) / len(vals))
        classic_z = (500 - mean) / std if std > 0 else 0
        # Robust z-score should flag the outlier more strongly
        assert robust_z is not None
        assert abs(robust_z) > abs(classic_z)


# ─── Error sanitization ───────────────────────────────────────────

class TestSanitizeError:
    """Tests for BaseCollector._sanitize_error()."""

    def test_env_var_scrubbing(self, monkeypatch):
        monkeypatch.setenv('FRED_API_KEY', 'my_secret_api_key_12345')
        from quant.collectors.base import BaseCollector
        msg = BaseCollector._sanitize_error('Failed with key my_secret_api_key_12345')
        assert 'my_secret_api_key_12345' not in msg
        assert '***' in msg

    def test_url_param_scrubbing(self):
        from quant.collectors.base import BaseCollector
        msg = BaseCollector._sanitize_error(
            'HTTP 403: https://api.example.com?api_key=LEAKED123&file_type=json')
        assert 'LEAKED123' not in msg
        assert 'api_key=***' in msg
        assert 'file_type=json' in msg

    def test_truncation(self):
        from quant.collectors.base import BaseCollector
        msg = BaseCollector._sanitize_error('x' * 600)
        assert len(msg) <= 515  # 500 + len('...[truncated]')
        assert msg.endswith('...[truncated]')

    def test_none_empty_passthrough(self):
        from quant.collectors.base import BaseCollector
        assert BaseCollector._sanitize_error(None) is None
        assert BaseCollector._sanitize_error('') == ''

    def test_short_values_preserved(self, monkeypatch):
        monkeypatch.setenv('SHORT_KEY', 'abc')
        from quant.collectors.base import BaseCollector
        msg = BaseCollector._sanitize_error('error abc happened')
        assert 'abc' in msg  # len < 8, should not be scrubbed


# ─── Incremental normalize ────────────────────────────────────────

class TestIncrementalNormalize:
    """Tests for the subquery wrapping in _process_domain()."""

    def test_latest_only_uses_row_number_wrapper(self):
        """latest_only mode must use ROW_NUMBER() per (metric, asset) to fetch
        the last 91 observations, not a 91-calendar-day date filter.

        This ensures sparse monthly series (e.g. M2) still accumulate enough
        points for z-score computation even though a 91-day window yields only
        ~3 rows for monthly data.
        """
        from unittest.mock import patch, MagicMock
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.__enter__ = lambda s: mock_conn
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value = mock_cur
        mock_cur.fetchall.return_value = []

        with patch('quant.normalize.get_conn', return_value=mock_conn):
            from quant.normalize import _process_domain
            _process_domain('test', 'SELECT ts, metric, asset, value FROM quant.macro',
                            dry_run=True, latest_only=True)

        sql = mock_cur.execute.call_args[0][0]
        # Must use ROW_NUMBER() partitioned by metric/asset — not a date filter
        assert 'ROW_NUMBER()' in sql
        assert 'PARTITION BY metric, asset' in sql
        assert 'rn <= 91' in sql
        # Must NOT use a calendar-day filter
        assert "INTERVAL '91 days'" not in sql

    def test_latest_only_preserves_output_columns(self):
        """The wrapper SQL must SELECT ts, metric, asset, value (same columns
        as the original domain query) so downstream grouping logic is unchanged.
        """
        from unittest.mock import patch, MagicMock
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.__enter__ = lambda s: mock_conn
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value = mock_cur
        mock_cur.fetchall.return_value = []

        with patch('quant.normalize.get_conn', return_value=mock_conn):
            from quant.normalize import _process_domain
            _process_domain('test', 'SELECT ts, metric, asset, value FROM quant.macro',
                            dry_run=True, latest_only=True)

        sql = mock_cur.execute.call_args[0][0]
        # Outer SELECT must include the four required columns
        assert 'SELECT ts, metric, asset, value' in sql

    def test_full_mode_uses_original(self):
        from unittest.mock import patch, MagicMock
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.__enter__ = lambda s: mock_conn
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value = mock_cur
        mock_cur.fetchall.return_value = []

        original = 'SELECT ts, metric, asset, value FROM quant.macro'
        with patch('quant.normalize.get_conn', return_value=mock_conn):
            from quant.normalize import _process_domain
            _process_domain('test', original, dry_run=True, latest_only=False)

        sql = mock_cur.execute.call_args[0][0]
        assert sql == original


# ─── Google Trends overlapping-window stitching ─────────────

class TestGoogleTrendsStitching:
    """Unit tests for stitch_overlapping_windows() — no network/DB needed.

    The function is pure: it takes a list of {date_str: float} dicts and
    returns one merged, rescaled dict.  Tests verify:
    - Two-window rescaling with a known scale mismatch produces a continuous,
      correctly-scaled series.
    - The most-recent window's values are preserved unchanged.
    - Overlap-too-short falls back to factor=1 (no rescaling).
    - Zero overlap mean falls back to factor=1.
    - Single-window input is returned as-is.
    - Empty input returns empty dict.
    - Three-window chain rescaling is correct.
    - Structural zeros (value == 0) are excluded from the overlap mean
      calculation (they are excluded before windows are built in the
      collector, but the stitch function itself is scale-agnostic).
    """

    @staticmethod
    def _w(dates_vals):
        """Build a {date_str: float} window from [(date_str, value), ...]."""
        return dict(dates_vals)

    def test_single_window_returned_unchanged(self):
        from quant.collectors.google_trends import stitch_overlapping_windows
        w = self._w([('2024-01-01', 50.0), ('2024-01-02', 80.0)])
        result = stitch_overlapping_windows([w])
        assert result == w

    def test_empty_input_returns_empty(self):
        from quant.collectors.google_trends import stitch_overlapping_windows
        assert stitch_overlapping_windows([]) == {}

    def test_two_windows_reference_unchanged(self):
        """W0 values must be identical in the output."""
        from quant.collectors.google_trends import stitch_overlapping_windows
        # W0 (reference): dates Jan 16-31
        w0 = {f'2024-01-{d:02d}': float(d) for d in range(16, 32)}
        # W1 (older): dates Jan 1-20, on half the scale
        w1 = {f'2024-01-{d:02d}': float(d) / 2 for d in range(1, 21)}
        result = stitch_overlapping_windows([w0, w1])
        # Every W0 date must appear at original value (W0 wins on overlap)
        for d, v in w0.items():
            assert result[d] == pytest.approx(v, abs=1e-9), \
                f"W0 value changed for {d}: expected {v}, got {result[d]}"

    def test_two_windows_scale_discontinuity_fixed(self):
        """W1 (2× downscaled) must be rescaled to ~W0's level.

        Setup:
          W0 dates: Jan 11-31, values = date-number (11..31)
          W1 dates: Jan 01-20, values = (date-number) / 2  ← half scale
          Overlap: Jan 11-20  (10 dates, well above MIN_OVERLAP_POINTS=5)

        Expected scale factor: mean(W0[11-20]) / mean(W1[11-20])
                              = mean(11..20) / mean(5.5..10)
                              = 15.5 / 7.75 = 2.0

        After rescaling, W1 values (Jan 1-10) should equal (date-number / 2) * 2
        = date-number.
        """
        from quant.collectors.google_trends import stitch_overlapping_windows

        w0 = {f'2024-01-{d:02d}': float(d) for d in range(11, 32)}
        w1 = {f'2024-01-{d:02d}': float(d) / 2.0 for d in range(1, 21)}
        result = stitch_overlapping_windows([w0, w1])

        # Non-overlap W1 dates (Jan 1-10) should be rescaled to ~original scale
        for d in range(1, 11):
            key = f'2024-01-{d:02d}'
            expected = float(d)  # original value before halving
            assert result[key] == pytest.approx(expected, rel=0.01), \
                f"Date {key}: expected ~{expected}, got {result[key]}"

    def test_output_is_sorted_by_date(self):
        from quant.collectors.google_trends import stitch_overlapping_windows
        w0 = {'2024-01-15': 50.0, '2024-01-20': 60.0}
        w1 = {'2024-01-10': 20.0, '2024-01-15': 25.0}
        result = stitch_overlapping_windows([w0, w1])
        assert list(result.keys()) == sorted(result.keys())

    def test_overlap_too_short_no_rescaling(self):
        """Fewer than MIN_OVERLAP_POINTS shared dates → scale factor 1.0."""
        from quant.collectors.google_trends import (
            stitch_overlapping_windows, MIN_OVERLAP_POINTS,
        )
        # W1 is on a 10× smaller scale but the overlap is too short to detect it
        overlap_count = MIN_OVERLAP_POINTS - 1  # deliberately below threshold
        overlap_dates = [f'2024-02-{d:02d}' for d in range(1, overlap_count + 1)]
        w0 = {d: 80.0 for d in overlap_dates}
        w1_only = {'2024-01-15': 4.0, '2024-01-20': 6.0}  # only in W1, low scale
        w1 = {**{d: 8.0 for d in overlap_dates}, **w1_only}  # 10× less than W0

        result = stitch_overlapping_windows([w0, w1])

        # With factor=1.0, W1-only dates should be stored as-is (low scale)
        for d, v in w1_only.items():
            assert result[d] == pytest.approx(v, abs=1e-9)

    def test_zero_overlap_mean_no_rescaling(self):
        """If W1's overlap values are all zero, factor falls back to 1.0."""
        from quant.collectors.google_trends import stitch_overlapping_windows, MIN_OVERLAP_POINTS
        overlap_dates = [f'2024-01-{d:02d}' for d in range(1, MIN_OVERLAP_POINTS + 2)]
        w0 = {d: 50.0 for d in overlap_dates}
        w1_unique = {'2023-12-15': 25.0}
        w1 = {**{d: 0.0 for d in overlap_dates}, **w1_unique}
        result = stitch_overlapping_windows([w0, w1])
        # factor=1.0 → W1 unique value unchanged
        assert result['2023-12-15'] == pytest.approx(25.0)

    def test_three_window_chain_rescaling(self):
        """Chain: W2 rescaled via W1 (already rescaled via W0).

        Setup (all series represent the same underlying signal):
          W0: dates 21-30, values = 21..30
          W1: dates 11-25, values = (11..25) * 0.5   ← half scale of W0
          W2: dates  1-15, values = (1..15) * 0.25   ← quarter scale of W0

        After stitching:
          W1 gets scaled by 2 (to match W0 in overlap 21-25).
          W2 gets scaled by 4 (quarter → full, via the already-rescaled W1).
          W2-only dates (1-10) should end up ≈ (1..10) — full scale.
        """
        from quant.collectors.google_trends import stitch_overlapping_windows

        w0 = {f'2024-01-{d:02d}': float(d)        for d in range(21, 31)}
        w1 = {f'2024-01-{d:02d}': float(d) * 0.5  for d in range(11, 26)}
        w2 = {f'2024-01-{d:02d}': float(d) * 0.25 for d in range(1,  16)}

        result = stitch_overlapping_windows([w0, w1, w2])

        # W2-only dates (1-10) should be back on W0's scale (≈ date-number)
        for d in range(1, 11):
            key = f'2024-01-{d:02d}'
            expected = float(d)
            assert result[key] == pytest.approx(expected, rel=0.05), \
                f"Chain rescale failed for {key}: expected ~{expected}, got {result[key]}"

    def test_w0_dominates_on_overlap_dates(self):
        """In the overlap region, the most-recent window (W0) wins."""
        from quant.collectors.google_trends import stitch_overlapping_windows
        overlap = [f'2024-01-{d:02d}' for d in range(11, 21)]
        w0 = {d: 70.0 for d in overlap}
        w1 = {d: 35.0 for d in overlap}
        w1.update({'2024-01-05': 10.0})
        result = stitch_overlapping_windows([w0, w1])
        for d in overlap:
            assert result[d] == pytest.approx(70.0)

    def test_compute_scale_factor_direct(self):
        """Unit test for the internal helper."""
        from quant.collectors.google_trends import _compute_scale_factor
        prev = {'2024-01-01': 80.0, '2024-01-02': 80.0, '2024-01-03': 80.0,
                '2024-01-04': 80.0, '2024-01-05': 80.0}
        curr = {'2024-01-01': 40.0, '2024-01-02': 40.0, '2024-01-03': 40.0,
                '2024-01-04': 40.0, '2024-01-05': 40.0}
        overlap = sorted(set(prev) & set(curr))
        factor = _compute_scale_factor(prev, curr, overlap)
        assert factor == pytest.approx(2.0)

    def test_continuity_across_boundary(self):
        """The stitched series must not exhibit a scale jump at the W0/W1 boundary.

        We manufacture a smooth linear signal on W0's scale (value = day-of-year).
        W1 is the same signal but halved.  After stitching the output should be
        smooth (no visible jump) at the boundary between the two windows.

        The overlap must be >= MIN_OVERLAP_POINTS (5) for rescaling to activate.
        We use a 10-day overlap (Jan 16-25) which is well above the threshold.
        """
        from quant.collectors.google_trends import stitch_overlapping_windows

        # W0: days 16-30 on full scale  (15 dates)
        w0 = {f'2024-01-{d:02d}': float(d) for d in range(16, 31)}
        # W1: days 1-25 on half scale (25 dates; overlap with W0 = Jan 16-25 = 10 dates)
        w1 = {f'2024-01-{d:02d}': float(d) / 2.0 for d in range(1, 26)}

        result = stitch_overlapping_windows([w0, w1])

        # Day 15 (W1-only after stitching, rescaled) should be ≈ 15.0 (full scale)
        # Day 16 (first W0 date) is exactly 16.0
        v15 = result.get('2024-01-15')
        v16 = result.get('2024-01-16')
        assert v15 is not None and v16 is not None, \
            f"Missing boundary dates: v15={v15}, v16={v16}"
        # Rescaled v15 ≈ 15.0; v16 = 16.0 → difference should be ~1 (smooth step)
        assert abs(v16 - v15) < 3.0, \
            f"Scale jump at boundary: v15={v15:.2f}, v16={v16:.2f}"


# ═══════════════════════════════════════════════════════════════════
# INTEGRATION TESTS — require production database
# Run with: pytest tests/test_quant.py -m integration
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.integration
class TestQuant_APISmoke:
    """Smoke tests against the live QE API endpoints."""

    @pytest.fixture(autouse=True)
    def setup(self):
        """Set up API base URL and auth."""
        import os
        self.base = os.environ.get('CONVEXITY_URL', '')
        self.key = os.environ.get('CONVEXITY_API_KEY', '')
        if not self.base or not self.key:
            pytest.skip("CONVEXITY_URL and CONVEXITY_API_KEY not set")

    def _get(self, path):
        import requests
        resp = requests.get(
            f"{self.base}/api/quant{path}",
            headers={'X-API-Key': self.key},
            timeout=30,
        )
        return resp

    def test_health(self):
        resp = self._get('/health')
        assert resp.status_code == 200
        data = resp.json()
        assert 'freshness' in data

    def test_snapshot(self):
        resp = self._get('/snapshot')
        assert resp.status_code == 200
        data = resp.json()
        assert 'regime' in data
        assert 'anomalies' in data
        assert 'top_correlations' in data
        assert 'leadlag_signals' in data
        # weekly lead-lag and chains
        assert 'leadlag_signals_weekly' in data
        assert 'chains' in data
        assert isinstance(data['chains'], list)
        # Check percentile is present on anomalies
        if data['anomalies']:
            assert 'percentile' in data['anomalies'][0]
        # Check chains have required fields
        if data['chains']:
            ch = data['chains'][0]
            assert 'chain' in ch
            assert 'chain_strength' in ch
            assert 'weakest_link' in ch

    def test_snapshot_has_unconditional_baseline(self):
        """Conditional endpoint should return unconditional_baseline."""
        resp = self._get('/conditional?conditions=VIX:gt:0&target=BTC&horizon=30')
        assert resp.status_code == 200
        data = resp.json()
        assert 'unconditional_baseline' in data
        if data.get('unconditional_baseline'):
            bl = data['unconditional_baseline']
            assert 'mean' in bl
            assert 'median' in bl
            assert 'n' in bl

    def test_correlations(self):
        resp = self._get('/correlations?metric=BTC&window=60')
        assert resp.status_code == 200
        data = resp.json()
        assert 'data' in data or 'count' in data

    def test_leadlag(self):
        resp = self._get('/leadlag?target=BTC')
        assert resp.status_code == 200

    def test_leadlag_weekly(self):
        resp = self._get('/leadlag?target=BTC&frequency=weekly')
        assert resp.status_code == 200
        data = resp.json()
        assert 'data' in data
        # Weekly results should have frequency field
        if data['data']:
            assert data['data'][0].get('frequency') == 'weekly'

    def test_leadlag_cross_asset_targets(self):
        """New cross-asset lead-lag targets should work."""
        for target in ['SPX', 'GOLD', 'YIELD_2Y']:
            resp = self._get(f'/leadlag?target={target}')
            assert resp.status_code == 200, f"Failed for target={target}"

    def test_chain_endpoint(self):
        for chain in ['monetary_policy', 'dollar_liquidity', 'risk_appetite']:
            resp = self._get(f'/chain?chain={chain}')
            assert resp.status_code == 200, f"Failed for chain={chain}"
            data = resp.json()
            assert 'chain_strength' in data
            assert 'weakest_link' in data
            assert 'links' in data
            assert len(data['links']) >= 3

    def test_chain_weekly(self):
        resp = self._get('/chain?chain=monetary_policy&frequency=weekly')
        assert resp.status_code == 200
        data = resp.json()
        assert data['frequency'] == 'weekly'

    def test_chain_invalid(self):
        resp = self._get('/chain?chain=nonexistent')
        assert resp.status_code == 400

    def test_regime(self):
        resp = self._get('/regime')
        assert resp.status_code == 200
        data = resp.json()
        assert 'state' in data or 'regime' in data or 'current' in data

    def test_analogues(self):
        resp = self._get('/analogues?target=BTC')
        assert resp.status_code == 200
        data = resp.json()
        assert 'analogues' in data
        assert 'current_vector' in data
        # Check for momentum deltas
        if data.get('current_vector'):
            d7_keys = [k for k in data['current_vector'] if k.endswith('_D7')]
            assert len(d7_keys) > 0, "Analogue momentum deltas missing from current_vector"

    def test_zscores(self):
        resp = self._get('/zscores?window=90&min_abs=0')
        assert resp.status_code == 200

    def test_series(self):
        resp = self._get('/series?domain=macro&metric=NET_LIQUIDITY&limit=5')
        assert resp.status_code == 200

    def test_metrics(self):
        resp = self._get('/metrics')
        assert resp.status_code == 200


@pytest.mark.integration
class TestQuant_DataFreshness:
    """Verify QE data is fresh and engines are producing output."""

    @pytest.fixture(autouse=True)
    def setup(self):
        import os
        self.base = os.environ.get('CONVEXITY_URL', '')
        self.key = os.environ.get('CONVEXITY_API_KEY', '')
        if not self.base or not self.key:
            pytest.skip("CONVEXITY_URL and CONVEXITY_API_KEY not set")

    def _get(self, path):
        import requests
        return requests.get(
            f"{self.base}/api/quant{path}",
            headers={'X-API-Key': self.key},
            timeout=30,
        ).json()

    def test_health_no_stale_domains(self):
        """All domains should have data within 72 hours."""
        from datetime import datetime
        data = self._get('/health')
        for f in data.get('freshness', []):
            if not f.get('latest'):
                continue
            latest = datetime.fromisoformat(f['latest'].replace('Z', '+00:00'))
            age_hours = (datetime.now(latest.tzinfo) - latest).total_seconds() / 3600
            assert age_hours < 72, f"Domain {f['domain']} is {age_hours:.0f}h stale"

    def test_regime_exists(self):
        data = self._get('/snapshot')
        assert data.get('regime') is not None, "No regime data — HMM engine may not have run"
        assert data['regime']['probability'] > 0

    def test_correlations_recent(self):
        data = self._get('/snapshot')
        assert len(data.get('top_correlations', [])) > 0, "No correlations — engine may not have run"

    def test_net_liquidity_exists(self):
        data = self._get('/series?domain=macro&metric=NET_LIQUIDITY&limit=1')
        rows = data if isinstance(data, list) else data.get('rows', data.get('data', []))
        assert len(rows) > 0, "NET_LIQUIDITY not computed"

    def test_realized_vol_exists(self):
        data = self._get('/zscores?window=90&min_abs=0')
        rows = data if isinstance(data, list) else data.get('rows', data.get('zscores', []))
        rvol = [r for r in rows if 'REALIZED_VOL' in str(r.get('metric', ''))]
        assert len(rvol) > 0, "Realized vol z-scores missing"

    def test_etf_flows_exist(self):
        data = self._get('/series?domain=onchain&metric=ETF_BTC_NET_FLOW&limit=1')
        rows = data if isinstance(data, list) else data.get('rows', data.get('data', []))
        assert len(rows) > 0, "ETF flow data missing"

    def test_altcoin_season_exists(self):
        data = self._get('/zscores?window=90&min_abs=0')
        rows = data if isinstance(data, list) else data.get('rows', data.get('zscores', []))
        alt = [r for r in rows if 'ALTCOIN_SEASON' in str(r.get('metric', ''))]
        assert len(alt) > 0, "Altcoin season z-scores missing"


# ─── Reliability tests ─────────────────────────────────────────────

@pytest.mark.usefixtures("real_notification_senders")
class TestFailureAlert:
    """Immediate collector failure notification.

    These assert on the URL and headers send_ntfy builds and mock requests.post
    themselves, so they need the genuine sender rather than the
    recorder — otherwise mock_post.assert_called_once() would fail (or worse,
    a weaker assertion would pass against a stub).
    """

    def _make_collector(self, **kwargs):
        from quant.collectors.base import BaseCollector
        c = BaseCollector.__new__(BaseCollector)
        c.name = 'test_collector'
        c.dry_run = kwargs.get('dry_run', False)
        c._log_id = kwargs.get('log_id', 42)
        c._rows_upserted = 0
        c._collected_items = set()
        c.max_retries = 0
        c.expected_items = []
        c.expected_items_key = 'asset'
        return c

    @patch.dict('os.environ', {'NTFY_TOPIC': 'test-topic'})
    @patch('quant.collectors.base.BaseCollector._recent_error_exists', return_value=False)
    def test_send_failure_alert_sends_ntfy(self, mock_recent):
        c = self._make_collector()
        with patch('requests.post') as mock_post:
            c._send_failure_alert('DB connection refused')
            mock_post.assert_called_once()
            call_args = mock_post.call_args
            # Assert the CONTRACT (configured base + topic), not
            # the literal host. It was pinned to ntfy.sh, which not every host can
            # reach, so the constant is configurable and the test must not
            # re-pin it.
            from carrac.notifications.ntfy import NTFY_BASE
            assert call_args[0][0] == f'{NTFY_BASE}/test-topic'
            assert b'test_collector' in call_args[1]['data']
            assert call_args[1]['headers']['Title'] == 'QE Collector FAILED: test_collector'

    @patch.dict('os.environ', {}, clear=True)
    def test_send_failure_alert_skips_without_topic(self):
        c = self._make_collector()
        with patch('requests.post') as mock_post:
            c._send_failure_alert('some error')
            mock_post.assert_not_called()

    @patch.dict('os.environ', {'NTFY_TOPIC': 'test-topic'})
    def test_send_failure_alert_skips_dry_run(self):
        c = self._make_collector(dry_run=True)
        with patch('requests.post') as mock_post:
            c._send_failure_alert('some error')
            mock_post.assert_not_called()

    @patch.dict('os.environ', {'NTFY_TOPIC': 'test-topic'})
    @patch('quant.collectors.base.BaseCollector._recent_error_exists', return_value=False)
    def test_send_failure_alert_swallows_exceptions(self, mock_recent):
        c = self._make_collector()
        with patch('requests.post', side_effect=ConnectionError('timeout')):
            # Should not raise
            c._send_failure_alert('some error')

    @patch.dict('os.environ', {'NTFY_TOPIC': 'test-topic'})
    @patch('quant.collectors.base.BaseCollector._recent_error_exists', return_value=True)
    def test_rate_limit_suppresses_duplicate(self, mock_recent):
        c = self._make_collector()
        with patch('requests.post') as mock_post:
            c._send_failure_alert('some error')
            mock_post.assert_not_called()

    def test_run_calls_alert_on_failure(self):
        from quant.collectors.base import BaseCollector

        class FailingCollector(BaseCollector):
            name = 'failing'
            def collect(self):
                raise RuntimeError('boom')

        c = FailingCollector(dry_run=True, max_retries=0)
        with patch.object(c, '_send_failure_alert') as mock_alert:
            with pytest.raises(RuntimeError):
                c.run()
            mock_alert.assert_called_once()
            assert 'boom' in mock_alert.call_args[0][0]


class TestRetryBackoff:
    """Collector retry with exponential backoff."""

    def test_retry_succeeds_on_second_attempt(self):
        from quant.collectors.base import BaseCollector

        call_count = [0]
        class FlakeyCollector(BaseCollector):
            name = 'flakey'
            def collect(self):
                call_count[0] += 1
                if call_count[0] == 1:
                    raise ConnectionError('transient')

        c = FlakeyCollector(dry_run=True, max_retries=2)
        with patch('quant.collectors.base._time.sleep'):
            c.run()
        assert call_count[0] == 2

    def test_retry_exhausted_raises(self):
        from quant.collectors.base import BaseCollector

        class AlwaysFailCollector(BaseCollector):
            name = 'always_fail'
            def collect(self):
                raise ConnectionError('permanent')

        c = AlwaysFailCollector(dry_run=True, max_retries=2)
        with patch('quant.collectors.base._time.sleep'):
            with patch.object(c, '_send_failure_alert'):
                with pytest.raises(ConnectionError):
                    c.run()

    def test_no_retry_on_keyboard_interrupt(self):
        from quant.collectors.base import BaseCollector

        call_count = [0]
        class KBCollector(BaseCollector):
            name = 'kb'
            def collect(self):
                call_count[0] += 1
                raise KeyboardInterrupt()

        c = KBCollector(dry_run=True, max_retries=3)
        with patch.object(c, '_send_failure_alert'):
            with pytest.raises(KeyboardInterrupt):
                c.run()
        assert call_count[0] == 1

    def test_no_retry_on_value_error(self):
        from quant.collectors.base import BaseCollector

        call_count = [0]
        class VECollector(BaseCollector):
            name = 've'
            def collect(self):
                call_count[0] += 1
                raise ValueError('bad config')

        c = VECollector(dry_run=True, max_retries=3)
        with patch.object(c, '_send_failure_alert'):
            with pytest.raises(ValueError):
                c.run()
        assert call_count[0] == 1

    def test_retry_sleep_exponential(self):
        from quant.collectors.base import BaseCollector
        s0 = BaseCollector._retry_sleep(0)
        s1 = BaseCollector._retry_sleep(1)
        s2 = BaseCollector._retry_sleep(2)
        assert 2.0 <= s0 <= 3.0, f"attempt 0: {s0}"
        assert 4.0 <= s1 <= 5.0, f"attempt 1: {s1}"
        assert 8.0 <= s2 <= 9.0, f"attempt 2: {s2}"

    def test_retry_after_header_respected(self):
        import requests as req
        from quant.collectors.base import BaseCollector
        resp = MagicMock()
        resp.status_code = 429
        resp.headers = {'Retry-After': '30'}
        exc = req.exceptions.HTTPError(response=resp)
        result = BaseCollector._extract_retry_after(exc)
        assert result == 30.0

    def test_retry_after_capped(self):
        from quant.collectors.base import BaseCollector
        assert BaseCollector._retry_sleep(0, retry_after=300) == 120

    def test_rows_reset_between_retries(self):
        from quant.collectors.base import BaseCollector

        call_count = [0]
        class CountingCollector(BaseCollector):
            name = 'counting'
            def collect(self):
                call_count[0] += 1
                self._rows_upserted = 100
                if call_count[0] < 2:
                    raise ConnectionError('fail')

        c = CountingCollector(dry_run=True, max_retries=2)
        with patch('quant.collectors.base._time.sleep'):
            c.run()
        # After successful retry, rows should be 100 (from the successful run)
        assert c._rows_upserted == 100
        assert call_count[0] == 2

    def test_alert_only_on_final_failure(self):
        from quant.collectors.base import BaseCollector

        class AlwaysFailCollector(BaseCollector):
            name = 'always_fail'
            def collect(self):
                raise ConnectionError('fail')

        c = AlwaysFailCollector(dry_run=True, max_retries=2)
        with patch('quant.collectors.base._time.sleep'):
            with patch.object(c, '_send_failure_alert') as mock_alert:
                with pytest.raises(ConnectionError):
                    c.run()
                # Alert should be called exactly once (final failure only)
                mock_alert.assert_called_once()


class TestCompleteness:
    """Data completeness checks."""

    def _make_collector(self, expected=None, key='asset', dry_run=False):
        from quant.collectors.base import BaseCollector

        class TestCollector(BaseCollector):
            name = 'test_completeness'
            expected_items = expected or []
            expected_items_key = key

        return TestCollector(dry_run=dry_run)

    def test_completeness_partial(self):
        c = self._make_collector(expected=['BTC', 'ETH', 'SOL'])
        c._collected_items = {'BTC', 'ETH'}
        result = c._track_completeness()
        assert result['expected'] == 3
        assert result['actual'] == 2
        assert result['missing'] == ['SOL']

    def test_completeness_empty_expected(self):
        c = self._make_collector(expected=[])
        result = c._track_completeness()
        assert result is None

    def test_completeness_full_match(self):
        c = self._make_collector(expected=['BTC', 'ETH'])
        c._collected_items = {'BTC', 'ETH'}
        result = c._track_completeness()
        assert result['expected'] == 2
        assert result['actual'] == 2
        assert result['missing'] == []

    def test_completeness_stored_in_metadata(self):
        """Verify _log_finish includes metadata in SQL when completeness available."""
        from quant.collectors.base import BaseCollector

        class MetaCollector(BaseCollector):
            name = 'meta_test'
            expected_items = ['A', 'B']
            expected_items_key = 'asset'

        c = MetaCollector(dry_run=False)
        c._log_id = 99
        c._collected_items = {'A'}

        with patch('quant.collectors.base.get_conn') as mock_conn:
            mock_cur = MagicMock()
            mock_conn.return_value.__enter__ = MagicMock(return_value=MagicMock(cursor=MagicMock(return_value=mock_cur)))
            c._log_finish('success')
            # Verify the SQL was called with metadata containing completeness
            call_args = mock_cur.execute.call_args
            sql = call_args[0][0]
            params = call_args[0][1]
            assert 'metadata' in sql
            assert params[3] is not None  # metadata_json should not be None
            import json
            meta = json.loads(params[3])
            assert meta['expected'] == 2
            assert meta['actual'] == 1
            assert meta['missing'] == ['B']

    def test_completeness_metric_key(self):
        c = self._make_collector(expected=['FEAR_GREED', 'VIX'], key='metric')
        c._collected_items = {'FEAR_GREED'}
        result = c._track_completeness()
        assert result['actual'] == 1
        assert result['missing'] == ['VIX']

    def test_collector_expected_items_defined(self):
        """All production collectors should have expected_items set."""
        from quant.collectors.binance_daily import BinanceDailyCollector
        from quant.collectors.fear_greed import FearGreedCollector
        from quant.collectors.fred_macro import FredMacroCollector
        from quant.collectors.cot import CotCollector

        assert len(BinanceDailyCollector.expected_items) == 10
        assert len(FearGreedCollector.expected_items) == 1
        assert len(FredMacroCollector.expected_items) >= 20
        assert len(CotCollector.expected_items) == 6


class TestSignalHandlers:
    """Collector signal handlers."""

    def test_sigterm_logs_interrupted(self):
        import os, signal
        from quant.collectors.base import BaseCollector

        class SigCollector(BaseCollector):
            name = 'sig_test'
            def collect(self):
                os.kill(os.getpid(), signal.SIGTERM)

        c = SigCollector(dry_run=True, max_retries=0)
        with patch.object(c, '_log_finish') as mock_finish:
            with patch.object(c, '_send_failure_alert'):
                with pytest.raises(SystemExit) as exc_info:
                    c.run()
            assert exc_info.value.code == 128 + signal.SIGTERM
            # Signal handler calls _log_finish('interrupted'), then SystemExit
            # propagates to _NO_RETRY branch which also calls _log_finish('error')
            calls = mock_finish.call_args_list
            assert calls[0][0][0] == 'interrupted'

    def test_signal_handlers_restored(self):
        import signal
        from quant.collectors.base import BaseCollector

        class OkCollector(BaseCollector):
            name = 'ok_test'
            def collect(self):
                pass

        orig_term = signal.getsignal(signal.SIGTERM)
        orig_int = signal.getsignal(signal.SIGINT)
        c = OkCollector(dry_run=True, max_retries=0)
        c.run()
        assert signal.getsignal(signal.SIGTERM) == orig_term
        assert signal.getsignal(signal.SIGINT) == orig_int


class TestRequestHelper:
    """Rate limit _request() helper."""

    def _make_collector(self):
        from quant.collectors.base import BaseCollector
        c = BaseCollector.__new__(BaseCollector)
        c.name = 'req_test'
        c.max_retries = 0
        return c

    def test_non_429_passthrough(self):
        c = self._make_collector()
        mock_resp = MagicMock(status_code=200)
        with patch('requests.get', return_value=mock_resp) as mock_get:
            resp = c._request('http://example.com', timeout=5)
            assert resp.status_code == 200
            mock_get.assert_called_once()

    def test_429_retries_then_succeeds(self):
        c = self._make_collector()
        resp_429 = MagicMock(status_code=429, headers={})
        resp_200 = MagicMock(status_code=200)
        with patch('requests.get', side_effect=[resp_429, resp_429, resp_200]):
            with patch('quant.collectors.base._time.sleep'):
                resp = c._request('http://example.com', max_429_retries=3)
                assert resp.status_code == 200

    def test_429_exhausted_raises(self):
        import requests as req
        c = self._make_collector()
        resp_429 = MagicMock(status_code=429, headers={})
        resp_429.raise_for_status.side_effect = req.exceptions.HTTPError(response=resp_429)
        with patch('requests.get', return_value=resp_429):
            with patch('quant.collectors.base._time.sleep'):
                with pytest.raises(req.exceptions.HTTPError):
                    c._request('http://example.com', max_429_retries=2)


class TestCircuitBreaker:
    """Circuit breaker per collector."""

    def setup_method(self):
        """Clear breaker registry between tests."""
        from quant.collectors.base import BaseCollector
        BaseCollector._breakers.clear()

    def test_circuit_breaker_skips_after_threshold(self):
        from quant.collectors.base import BaseCollector

        class FailCollector(BaseCollector):
            name = 'cb_fail'
            def collect(self):
                raise ConnectionError('down')

        # Fail 3 times to trip the breaker
        for _ in range(3):
            c = FailCollector(dry_run=True, max_retries=0)
            with patch.object(c, '_send_failure_alert'):
                with pytest.raises(ConnectionError):
                    c.run()

        # 4th run should be skipped (breaker open)
        c = FailCollector(dry_run=True, max_retries=0)
        with patch.object(c, '_log_finish') as mock_finish:
            c.run()  # Should NOT raise
            mock_finish.assert_called_once()
            assert mock_finish.call_args[0][0] == 'skipped'

    def test_circuit_breaker_resets_on_success(self):
        from quant.collectors.base import BaseCollector

        call_count = [0]
        class FlakeyCollector(BaseCollector):
            name = 'cb_flakey'
            def collect(self):
                call_count[0] += 1
                if call_count[0] <= 2:
                    raise ConnectionError('flaky')

        # Fail twice
        for _ in range(2):
            c = FlakeyCollector(dry_run=True, max_retries=0)
            with patch.object(c, '_send_failure_alert'):
                with pytest.raises(ConnectionError):
                    c.run()

        # 3rd call succeeds — breaker should reset
        c = FlakeyCollector(dry_run=True, max_retries=0)
        c.run()

        # 4th call should NOT be skipped (breaker was reset)
        call_count[0] = 0  # Reset test counter
        c = FlakeyCollector(dry_run=True, max_retries=0)
        with patch.object(c, '_send_failure_alert'):
            with pytest.raises(ConnectionError):
                c.run()  # Should actually attempt, not skip


# ─── formula-parser primitives ───────────────────────────────

@needs_numpy
class TestFormulaU10Parser:
    """Parse-only tests for dot-notation primitives (no DB needed)."""

    def test_leadlag_with_member(self):
        from carrac.formula_eval import validate_formula
        r = validate_formula("leadlag('M2', 'BTC').projected_move > 0.5")
        assert r['valid'], r.get('error')

    def test_chain_with_member(self):
        from carrac.formula_eval import validate_formula
        r = validate_formula("chain('monetary_policy').strength < 0.2")
        assert r['valid'], r.get('error')

    def test_regime_prob_scalar(self):
        from carrac.formula_eval import validate_formula
        r = validate_formula("regime_prob('favorable') > 0.6")
        assert r['valid']

    def test_anomaly_active_no_args(self):
        from carrac.formula_eval import validate_formula
        r = validate_formula("anomaly_active() == 1")
        assert r['valid']

    def test_analogue_similarity_with_rank(self):
        from carrac.formula_eval import validate_formula
        r = validate_formula("analogue_similarity(3) > 0.8")
        assert r['valid']

    def test_compound_expression(self):
        from carrac.formula_eval import validate_formula
        expr = ("leadlag('M2', 'BTC').projected_move > 0.5 AND "
                "chain('risk_appetite').weakest_r < 0.15")
        r = validate_formula(expr)
        assert r['valid'], r.get('error')

    def test_double_quoted_string_also_parses(self):
        from carrac.formula_eval import validate_formula
        r = validate_formula('chain("dollar_liquidity").strength < 0.2')
        assert r['valid']

    def test_member_on_non_call_is_rejected_at_eval(self):
        # Parser accepts `close.foo` — evaluator rejects. Just verify parse.
        from carrac.formula_eval import validate_formula
        r = validate_formula("close.foo > 0")
        # Parse succeeds; eval would fail — that's by design (keeps parser simple)
        assert r['valid']

    def test_regression_old_formulas_still_parse(self):
        from carrac.formula_eval import validate_formula
        for expr in ('close > ema(close, 20)', 'rsi(14) < 30',
                     'crossover(ema(9), ema(21))', 'close[1] > close[0]'):
            r = validate_formula(expr)
            assert r['valid'], f"Regression failed on {expr!r}: {r.get('error')}"


class TestErrorsReachingLogsAreSanitized:
    """The Finnhub economic-calendar token must never reach the engine log in
    plaintext. (The token below is a synthetic stand-in of the same shape.)

    _sanitize_error is applied to DB storage and ntfy alerts, and the logger
    calls in BaseCollector's retry loop must pass the exception through it
    too rather than logging it raw — a credential path.
    """

    LEAKED = ('403 Client Error: Forbidden for url: '
              'https://finnhub.io/api/v1/calendar/economic'
              '?from=2026-08-22&to=2026-09-05&token=fakefinnhubtoken0123456789abcdefghij')

    def test_the_exact_string_that_leaked_is_redacted(self):
        from quant.collectors.base import BaseCollector
        out = BaseCollector._sanitize_error(self.LEAKED)
        assert 'fakefinnhubtoken' not in out
        assert 'token=***' in out
        assert 'finnhub.io' in out, 'redaction should keep the message diagnosable'

    def test_traceback_path_is_sanitized_too(self):
        """exc_info=True renders the exception's str() as the traceback's last
        line, so swapping the f-string alone would not have closed it."""
        from quant.collectors.base import BaseCollector
        try:
            raise RuntimeError(self.LEAKED)
        except RuntimeError as e:
            out = BaseCollector._sanitize_exc(e)
        assert 'fakefinnhubtoken' not in out
        assert 'token=***' in out
        assert 'RuntimeError' in out, 'the traceback itself should survive'
        assert 'Traceback' in out

    def test_no_collector_log_call_passes_a_raw_exception(self):
        """The guard for the whole class, not just the one line found."""
        import inspect
        import re
        from quant.collectors import base
        src = inspect.getsource(base.BaseCollector.run)
        bad = re.findall(r'logger\.\w+\([^)]*\{e\}', src)
        assert not bad, f'raw exception interpolated into a log call: {bad}'
        assert 'exc_info=True' not in src, \
            'exc_info renders the raw exception message — use _sanitize_exc'

