"""
Tests for carrac.facts.mayer — Mayer multiple + historical percentile.

TDD: these tests were written first; implementation follows.
"""

import pytest
from carrac.facts.mayer import mayer, mayer_card, WINDOW


# ---------------------------------------------------------------------------
# Insufficient data
# ---------------------------------------------------------------------------

class TestMayerInsufficientData:
    def test_empty_returns_none(self):
        assert mayer([]) is None

    def test_one_bar_returns_none(self):
        assert mayer([100.0]) is None

    def test_one_short_of_window_returns_none(self):
        # WINDOW - 1 bars must return None
        assert mayer([100.0] * (WINDOW - 1)) is None

    def test_exact_window_returns_result(self):
        # Exactly WINDOW bars is the minimum — must NOT return None
        result = mayer([100.0] * WINDOW)
        assert result is not None


# ---------------------------------------------------------------------------
# Strictly increasing series: last multiple is the max → percentile = 100
# ---------------------------------------------------------------------------

class TestMayerStrictlyIncreasing:
    """
    Construction: integers 1..WINDOW then a large spike (WINDOW * 100).
    Total: WINDOW + 1 bars — strictly increasing.
    Two valid multiples:
      m[WINDOW-1] = WINDOW / mean(1..WINDOW) = 1400 / 700.5 ≈ 2.0
      m[WINDOW]   = WINDOW*100 / mean(2..WINDOW, WINDOW*100)
                  = 140000 / ((980699 + 140000) / 1400) ≈ 174.9
    Last multiple >> first → last is max → percentile = 100.
    """

    def test_percentile_is_100(self):
        close = list(range(1, WINDOW + 1)) + [WINDOW * 100]
        result = mayer(close)
        assert result is not None
        assert result['percentile'] == 100

    def test_multiple_greater_than_one(self):
        close = list(range(1, WINDOW + 1)) + [WINDOW * 100]
        result = mayer(close)
        assert result is not None
        assert result['multiple'] > 1.0


# ---------------------------------------------------------------------------
# Hand-computed percentile: last multiple is neither max nor min
# ---------------------------------------------------------------------------

class TestMayerMiddlePercentile:
    """
    Construction: WINDOW bars flat at 10.0, then spike to 20.0, then back to 10.0.
    Three valid multiples (at indices WINDOW-1, WINDOW, WINDOW+1):

      MA common denominator = 14010 / 1400 = 10.007142…   (same for bars WINDOW and WINDOW+1
                                                             because sum(10*1398 + 20 + 10)
                                                             = sum(10*1399 + 20) = 14010)

      m[WINDOW-1] = 10.0 / 10.0        = 1.000000  (flat window)
      m[WINDOW]   = 20.0 / 10.007142…  ≈ 1.998571
      m[WINDOW+1] = 10.0 / 10.007142…  ≈ 0.999286  ← last

    Sorted: 0.999286, 1.0, 1.998571
    count(x ≤ 0.999286) = 1   (only itself)
    n = 3
    percentile = round(100 * 1/3) = 33
    """

    def _close(self):
        return [10.0] * WINDOW + [20.0, 10.0]

    def test_percentile_is_33(self):
        result = mayer(self._close())
        assert result is not None
        assert result['percentile'] == 33

    def test_percentile_strictly_between_0_and_100(self):
        result = mayer(self._close())
        assert result is not None
        assert 0 < result['percentile'] < 100

    def test_multiple_is_below_1(self):
        # Last bar dipped back below MA → multiple < 1
        result = mayer(self._close())
        assert result is not None
        # 10.0 / (14010/1400) = 1400/1401 ≈ 0.999
        assert result['multiple'] == pytest.approx(1400 / 1401, rel=1e-3)


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------

class TestMayerDeterminism:
    def test_same_input_same_output(self):
        close = list(range(1, WINDOW + 1)) + [WINDOW * 100]
        r1 = mayer(close)
        r2 = mayer(close)
        assert r1 == r2

    def test_same_input_flat_series(self):
        close = [50.0] * WINDOW
        r1 = mayer(close)
        r2 = mayer(close)
        assert r1 == r2


# ---------------------------------------------------------------------------
# Return-value shape and types
# ---------------------------------------------------------------------------

class TestMayerReturnShape:
    def test_returns_dict(self):
        result = mayer([100.0] * WINDOW)
        assert isinstance(result, dict)

    def test_has_multiple_key(self):
        assert 'multiple' in mayer([100.0] * WINDOW)

    def test_has_percentile_key(self):
        assert 'percentile' in mayer([100.0] * WINDOW)

    def test_multiple_is_float(self):
        result = mayer([100.0] * WINDOW)
        assert isinstance(result['multiple'], float)

    def test_percentile_is_int(self):
        result = mayer([100.0] * WINDOW)
        assert isinstance(result['percentile'], int)

    def test_percentile_in_0_to_100(self):
        result = mayer([100.0] * WINDOW)
        assert 0 <= result['percentile'] <= 100

    def test_flat_series_multiple_is_1(self):
        # Flat series: every price == MA → multiple = 1.0 exactly
        result = mayer([100.0] * WINDOW)
        assert result is not None
        assert result['multiple'] == pytest.approx(1.0, rel=1e-6)

    def test_multiple_rounded_to_3dp(self):
        # 3-decimal rounding: 1400/1401 = 0.999286… → rounds to 0.999
        result = mayer([10.0] * WINDOW + [20.0, 10.0])
        assert result is not None
        assert result['multiple'] == 0.999


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestMayerEdgeCases:
    def test_zero_prices_returns_none(self):
        # All-zero prices → MA = 0.0 everywhere → divide-by-zero guard
        result = mayer([0.0] * WINDOW)
        assert result is None

    def test_single_valid_multiple_percentile_100(self):
        # Exactly WINDOW bars: only 1 multiple, it equals itself → percentile = 100
        result = mayer([100.0] * WINDOW)
        assert result is not None
        assert result['percentile'] == 100


# ---------------------------------------------------------------------------
# mayer_card — legacy-shaped dict for the weekly-signal engine / sentiment path.
# Replaces the CoinMetrics-CSV fetch_mayer_multiple() with a local-OHLCV compute.
# Contract: returns the same keys as the legacy CoinMetrics path
# ({'mayer_multiple', 'price', 'ma_200w', 'zone'} + 'percentile'), and on
# insufficient data returns a dict with mayer_multiple=None (NEVER None itself),
# so callers can safely do mayer_data.get('mayer_multiple').
# ---------------------------------------------------------------------------

class TestMayerCard:
    def test_insufficient_returns_dict_with_none_multiple(self):
        # Fewer than WINDOW bars must still return a DICT (not None) with
        # mayer_multiple=None — the weekly engine does mayer_data.get(...).
        result = mayer_card([100.0] * (WINDOW - 1))
        assert isinstance(result, dict)
        assert result['mayer_multiple'] is None
        assert result['zone'] == 'unknown'

    def test_has_legacy_keys(self):
        result = mayer_card([100.0] * WINDOW)
        for key in ('mayer_multiple', 'price', 'ma_200w', 'zone'):
            assert key in result

    def test_flat_series_multiple_is_1_zone_normal(self):
        # Flat window → multiple = 1.0 → zone 'normal' (1.0 is in [1.0, 2.0)).
        result = mayer_card([100.0] * WINDOW)
        assert result['mayer_multiple'] == pytest.approx(1.0, rel=1e-6)
        assert result['zone'] == 'normal'

    def test_price_is_last_close(self):
        result = mayer_card([100.0] * (WINDOW - 1) + [123.0])
        assert result['price'] == pytest.approx(123.0)

    def test_ma_200w_is_mean_of_window(self):
        result = mayer_card([100.0] * WINDOW)
        assert result['ma_200w'] == pytest.approx(100.0)

    def test_bubble_zone_when_multiple_above_2_4(self):
        # Window flat at 10, final bar spikes to 100 → multiple ≈ 9.9 → bubble.
        result = mayer_card([10.0] * WINDOW + [100.0])
        assert result['mayer_multiple'] > 2.4
        assert result['zone'] == 'bubble'

    def test_deep_discount_zone_when_multiple_below_0_8(self):
        # Final bar dumps to 5 vs a ~10 MA → multiple ≈ 0.5 → deep_discount.
        result = mayer_card([10.0] * WINDOW + [5.0])
        assert result['mayer_multiple'] < 0.8
        assert result['zone'] == 'deep_discount'

    def test_accepts_pandas_series(self):
        # The DB hands us df['close'] (a pandas Series) — must work, not just lists.
        import pandas as pd
        result = mayer_card(pd.Series([100.0] * WINDOW))
        assert result['mayer_multiple'] == pytest.approx(1.0, rel=1e-6)
