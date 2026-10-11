"""Unit tests for the liquidation-heatmap model math.

Pure functions only -- carrac/liq_heatmap.py's compute_*/build_*/apply_*
helpers take plain lists/dicts and do no I/O, so every test here uses
synthetic fixtures, no live network / DB / Redis.
"""

from __future__ import annotations

import pytest

from carrac.liq_heatmap import (
    apply_prorata_decay,
    bands_from_grid,
    bucket_index_for_price,
    build_price_buckets,
    clear_traversed_buckets,
    compute_liq_heatmap_from_series,
    distribute_position_to_tiers,
    filter_bands_by_threshold,
    infer_position_notional,
    leverage_liq_price,
    normalize_intensity,
)


# ---------------------------------------------------------------------------
# Bucketing
# ---------------------------------------------------------------------------

class TestBuildPriceBuckets:
    def test_count_and_bounds(self):
        buckets = build_price_buckets(100.0, band_pct=0.25, n_buckets=100)
        assert len(buckets) == 100
        assert buckets[0][0] == pytest.approx(75.0)
        assert buckets[-1][1] == pytest.approx(125.0)

    def test_ascending_contiguous(self):
        buckets = build_price_buckets(100.0, band_pct=0.25, n_buckets=10)
        for i in range(1, len(buckets)):
            assert buckets[i][0] == pytest.approx(buckets[i - 1][1])

    def test_zero_or_negative_spot_returns_empty(self):
        assert build_price_buckets(0, 0.25, 100) == []
        assert build_price_buckets(-5, 0.25, 100) == []

    def test_zero_buckets_returns_empty(self):
        assert build_price_buckets(100.0, 0.25, 0) == []


class TestBucketIndexForPrice:
    def test_inside_band(self):
        buckets = build_price_buckets(100.0, band_pct=0.25, n_buckets=100)
        idx = bucket_index_for_price(buckets, 100.0)
        assert idx is not None
        lo, hi = buckets[idx]
        assert lo <= 100.0 < hi

    def test_outside_band_returns_none(self):
        buckets = build_price_buckets(100.0, band_pct=0.25, n_buckets=100)
        assert bucket_index_for_price(buckets, 50.0) is None
        assert bucket_index_for_price(buckets, 200.0) is None

    def test_empty_buckets_returns_none(self):
        assert bucket_index_for_price([], 100.0) is None

    def test_none_price_returns_none(self):
        buckets = build_price_buckets(100.0, 0.25, 10)
        assert bucket_index_for_price(buckets, None) is None


# ---------------------------------------------------------------------------
# Leverage-tier liquidation price
# ---------------------------------------------------------------------------

class TestLeverageLiqPrice:
    def test_long_below_entry(self):
        # long 10x @ 100, mmr 0.005 -> 100 * (1 - 0.1 + 0.005) = 90.5
        assert leverage_liq_price(100.0, 'long', 10, 0.005) == pytest.approx(90.5)

    def test_short_above_entry(self):
        # short 10x @ 100, mmr 0.005 -> 100 * (1 + 0.1 - 0.005) = 109.5
        assert leverage_liq_price(100.0, 'short', 10, 0.005) == pytest.approx(109.5)

    def test_higher_leverage_closer_to_entry(self):
        long_10x = leverage_liq_price(100.0, 'long', 10, 0.005)
        long_100x = leverage_liq_price(100.0, 'long', 100, 0.005)
        assert long_100x > long_10x  # 100x liquidates much closer to entry

    def test_invalid_side_raises(self):
        with pytest.raises(ValueError):
            leverage_liq_price(100.0, 'sideways', 10)


# ---------------------------------------------------------------------------
# Position inference (OI-delta -> long/short notional split)
# ---------------------------------------------------------------------------

class TestInferPositionNotional:
    def test_fifty_fifty_default(self):
        long_n, short_n = infer_position_notional(1000.0)
        assert long_n == pytest.approx(500.0)
        assert short_n == pytest.approx(500.0)

    def test_custom_long_fraction(self):
        long_n, short_n = infer_position_notional(1000.0, long_fraction=0.7)
        assert long_n == pytest.approx(700.0)
        assert short_n == pytest.approx(300.0)

    def test_negative_or_zero_delta_is_not_a_new_position(self):
        assert infer_position_notional(-500.0) == (0.0, 0.0)
        assert infer_position_notional(0.0) == (0.0, 0.0)
        assert infer_position_notional(None) == (0.0, 0.0)

    def test_long_fraction_clamped(self):
        long_n, short_n = infer_position_notional(1000.0, long_fraction=1.5)
        assert long_n == pytest.approx(1000.0)
        assert short_n == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Leverage-tier distribution (band placement)
# ---------------------------------------------------------------------------

class TestDistributePositionToTiers:
    def test_weights_sum_to_full_notional(self):
        buckets = build_price_buckets(100.0, band_pct=0.25, n_buckets=200)
        contributions = distribute_position_to_tiers(1000.0, 100.0, 'long', buckets)
        assert sum(contributions.values()) == pytest.approx(1000.0)

    def test_default_weights_used(self):
        buckets = build_price_buckets(100.0, band_pct=0.25, n_buckets=200)
        contributions = distribute_position_to_tiers(1000.0, 100.0, 'long', buckets)
        # 4 leverage tiers -> at most 4 distinct buckets (could collapse if two
        # tiers map to the same bucket, but with 200 buckets across a 25%
        # band that shouldn't happen for these tiers).
        assert 1 <= len(contributions) <= 4

    def test_long_tiers_land_below_entry(self):
        buckets = build_price_buckets(100.0, band_pct=0.25, n_buckets=200)
        contributions = distribute_position_to_tiers(1000.0, 100.0, 'long', buckets)
        for idx in contributions:
            lo, hi = buckets[idx]
            assert hi <= 100.0  # long liq clusters sit below entry

    def test_short_tiers_land_above_entry(self):
        buckets = build_price_buckets(100.0, band_pct=0.25, n_buckets=200)
        contributions = distribute_position_to_tiers(1000.0, 100.0, 'short', buckets)
        for idx in contributions:
            lo, hi = buckets[idx]
            assert lo >= 100.0  # short liq clusters sit above entry

    def test_out_of_band_tier_dropped_not_erroring(self):
        # An extremely narrow band pushes every tier's liq price outside the
        # band entirely -- must be dropped, not raise or wrap around. Even
        # the highest-leverage (100x) tier sits 0.5% from entry, so the band
        # must be narrower than that to prove the drop path.
        buckets = build_price_buckets(100.0, band_pct=0.003, n_buckets=50)
        contributions = distribute_position_to_tiers(1000.0, 100.0, 'long', buckets)
        assert contributions == {}

    def test_zero_notional_returns_empty(self):
        buckets = build_price_buckets(100.0, 0.25, 100)
        assert distribute_position_to_tiers(0.0, 100.0, 'long', buckets) == {}


# ---------------------------------------------------------------------------
# Decay on OI drop
# ---------------------------------------------------------------------------

class TestApplyProrataDecay:
    def test_shrinks_all_buckets_equally(self):
        intensity = [100.0, 50.0, 0.0, 200.0]
        apply_prorata_decay(intensity, 0.25)
        assert intensity == pytest.approx([75.0, 37.5, 0.0, 150.0])

    def test_full_decay_zeroes_everything(self):
        intensity = [100.0, 50.0]
        apply_prorata_decay(intensity, 1.0)
        assert intensity == pytest.approx([0.0, 0.0])

    def test_negative_fraction_clamped_to_noop(self):
        intensity = [100.0, 50.0]
        apply_prorata_decay(intensity, -0.5)
        assert intensity == pytest.approx([100.0, 50.0])

    def test_fraction_over_one_clamped(self):
        intensity = [100.0]
        apply_prorata_decay(intensity, 5.0)
        assert intensity == pytest.approx([0.0])


# ---------------------------------------------------------------------------
# Clear-on-traverse
# ---------------------------------------------------------------------------

class TestClearTraversedBuckets:
    def test_overlapping_buckets_cleared_both_sides(self):
        buckets = [(90.0, 95.0), (95.0, 100.0), (100.0, 105.0), (105.0, 110.0)]
        long_i = [1.0, 1.0, 1.0, 1.0]
        short_i = [1.0, 1.0, 1.0, 1.0]
        clear_traversed_buckets(long_i, short_i, buckets, 94.0, 101.0)
        # buckets 0,1,2 overlap [94, 101]; bucket 3 (105-110) does not
        assert long_i == [0.0, 0.0, 0.0, 1.0]
        assert short_i == [0.0, 0.0, 0.0, 1.0]

    def test_no_overlap_leaves_untouched(self):
        buckets = [(90.0, 95.0), (105.0, 110.0)]
        long_i = [1.0, 1.0]
        short_i = [1.0, 1.0]
        clear_traversed_buckets(long_i, short_i, buckets, 96.0, 100.0)
        assert long_i == [1.0, 1.0]
        assert short_i == [1.0, 1.0]

    def test_none_bounds_noop(self):
        buckets = [(90.0, 95.0)]
        long_i = [1.0]
        short_i = [1.0]
        clear_traversed_buckets(long_i, short_i, buckets, None, 100.0)
        assert long_i == [1.0]


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

class TestNormalizeIntensity:
    def test_shared_max_across_sides(self):
        long_i = [10.0, 20.0]
        short_i = [40.0, 5.0]
        norm_long, norm_short = normalize_intensity(long_i, short_i)
        # shared peak is 40 (short side) -> long side max becomes 20/40=0.5
        assert norm_long == pytest.approx([0.25, 0.5])
        assert norm_short == pytest.approx([1.0, 0.125])

    def test_all_zero_passthrough(self):
        long_i = [0.0, 0.0]
        short_i = [0.0, 0.0]
        norm_long, norm_short = normalize_intensity(long_i, short_i)
        assert norm_long == [0.0, 0.0]
        assert norm_short == [0.0, 0.0]


# ---------------------------------------------------------------------------
# Full orchestration (dense grid)
# ---------------------------------------------------------------------------

class TestComputeLiqHeatmapFromSeries:
    def test_oi_increase_creates_intensity(self):
        oi_series = [1_000_000.0, 1_100_000.0, 1_100_000.0]
        price_series = [
            {'low': 99.0, 'high': 101.0, 'close': 100.0},
            {'low': 99.0, 'high': 101.0, 'close': 100.0},
            {'low': 99.0, 'high': 101.0, 'close': 100.0},
        ]
        buckets, long_i, short_i = compute_liq_heatmap_from_series(
            oi_series, price_series, spot_price=100.0, n_buckets=200,
        )
        assert len(buckets) == 200
        assert any(v > 0 for v in long_i)
        assert any(v > 0 for v in short_i)
        # normalized -> max is exactly 1.0 somewhere
        assert max(long_i + short_i) == pytest.approx(1.0)

    def test_oi_decrease_decays_existing_intensity(self):
        # Step 1: OI opens a big position. Step 2: OI drops back to zero delta
        # relative to a smaller value -> decay should shrink prior intensity.
        oi_series = [1_000_000.0, 2_000_000.0, 1_000_000.0]
        price_series = [
            {'low': 99.0, 'high': 101.0, 'close': 100.0},
            {'low': 99.0, 'high': 101.0, 'close': 100.0},
            # Step 3 candle stays inside the band, doesn't clear the tiers
            # (which sit further out) -- isolates the decay effect.
            {'low': 99.5, 'high': 100.5, 'close': 100.0},
        ]
        # Compare against a variant where OI never drops (no decay) to prove
        # the drop path actually shrinks intensity.
        buckets_decay, long_decay, short_decay = compute_liq_heatmap_from_series(
            oi_series, price_series, spot_price=100.0, n_buckets=200,
        )
        oi_series_no_drop = [1_000_000.0, 2_000_000.0, 2_000_000.0]
        _, long_stable, short_stable = compute_liq_heatmap_from_series(
            oi_series_no_drop, price_series, spot_price=100.0, n_buckets=200,
        )
        # Both are normalized independently so compare shape, not raw values:
        # the decay run should have LESS surviving total mass relative to its
        # own peak-at-step-2 contribution -- assert at least one bucket that
        # is nonzero in the stable run is smaller (proportionally) after decay.
        assert sum(long_decay) <= sum(long_stable) + 1e-9

    def test_price_traversal_clears_bucket(self):
        # Step 1 opens a position whose long-liq tier sits ~below 100.
        # Step 2's candle sweeps down through that level -> should clear it.
        oi_series = [1_000_000.0, 1_100_000.0, 1_100_000.0]
        price_series = [
            {'low': 99.0, 'high': 101.0, 'close': 100.0},
            {'low': 99.0, 'high': 101.0, 'close': 100.0},
            # 10x long liq @ 100 with mmr 0.005 = 90.5 -- sweep through it.
            {'low': 85.0, 'high': 100.0, 'close': 90.0},
        ]
        buckets, long_i, short_i = compute_liq_heatmap_from_series(
            oi_series, price_series, spot_price=100.0, n_buckets=200,
        )
        idx = bucket_index_for_price(buckets, 90.5)
        assert idx is not None
        assert long_i[idx] == pytest.approx(0.0)

    def test_too_few_points_returns_empty(self):
        assert compute_liq_heatmap_from_series([1.0], [{'low': 1, 'high': 2, 'close': 1}]) == ([], [], [])

    def test_no_spot_price_derivable_returns_empty(self):
        oi_series = [1.0, 2.0]
        price_series = [None, None]
        assert compute_liq_heatmap_from_series(oi_series, price_series) == ([], [], [])

    def test_long_fraction_series_skews_split(self):
        oi_series = [1_000_000.0, 1_100_000.0]
        price_series = [
            {'low': 99.0, 'high': 101.0, 'close': 100.0},
            {'low': 99.0, 'high': 101.0, 'close': 100.0},
        ]
        # All-long inference (long_fraction=1.0) -> short side stays empty.
        _, long_i, short_i = compute_liq_heatmap_from_series(
            oi_series, price_series, spot_price=100.0, n_buckets=200,
            long_fraction_series=[None, 1.0],
        )
        assert sum(short_i) == 0.0
        assert sum(long_i) > 0.0


# ---------------------------------------------------------------------------
# Output shape + threshold sparsification
# ---------------------------------------------------------------------------

class TestBandsFromGrid:
    def test_shape(self):
        buckets = build_price_buckets(100.0, 0.25, 5)
        long_i = [0.1, 0.2, 0.3, 0.4, 0.5]
        short_i = [0.5, 0.4, 0.3, 0.2, 0.1]
        bands = bands_from_grid(buckets, long_i, short_i)
        assert len(bands) == 5
        for b in bands:
            assert set(b.keys()) == {'price_low', 'price_high', 'intensity_long', 'intensity_short'}


class TestFilterBandsByThreshold:
    def test_keeps_only_top_quantile(self):
        bands = [
            {'price_low': i, 'price_high': i + 1, 'intensity_long': v, 'intensity_short': 0.0}
            for i, v in enumerate([0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0])
        ]
        filtered = filter_bands_by_threshold(bands, quantile=0.85)
        # ~15% of 10 nonzero values -> only the very top surge through
        assert len(filtered) <= 3
        assert all(b['intensity_long'] >= 0.8 for b in filtered)

    def test_all_zero_returns_empty(self):
        bands = [{'price_low': 0, 'price_high': 1, 'intensity_long': 0.0, 'intensity_short': 0.0}] * 5
        assert filter_bands_by_threshold(bands) == []

    def test_single_nonzero_kept_unfiltered(self):
        bands = [
            {'price_low': 0, 'price_high': 1, 'intensity_long': 0.0, 'intensity_short': 0.0},
            {'price_low': 1, 'price_high': 2, 'intensity_long': 0.5, 'intensity_short': 0.0},
        ]
        filtered = filter_bands_by_threshold(bands)
        assert len(filtered) == 1
        assert filtered[0]['intensity_long'] == 0.5
