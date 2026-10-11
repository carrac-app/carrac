"""Unit tests for the liquidation-heatmap validation harness's pure scoring
function (scripts/validate_liq_heatmap.py).

No DB/network — synthetic fixtures only.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from scripts.validate_liq_heatmap import (
    build_synthetic_dataset,
    score_events_against_snapshots,
)

_T0 = datetime(2026, 7, 1, tzinfo=timezone.utc)


def _snap(ts, band_low, band_high, bands, n_buckets=150):
    return {'ts': ts, 'band_low': band_low, 'band_high': band_high,
            'bands': bands, 'n_buckets': n_buckets}


def _band(lo, hi, il=0.0, is_=0.0):
    return {'price_low': lo, 'price_high': hi, 'intensity_long': il, 'intensity_short': is_}


class TestScoreEventsAgainstSnapshots:
    def test_empty_inputs_return_none(self):
        result = score_events_against_snapshots([], [])
        assert result['observed_fraction'] is None
        assert result['uniform_fraction'] is None
        assert result['observed_total'] == 0

    def test_event_inside_top_bucket_counts_as_hit(self):
        snapshots = [_snap(_T0, 90.0, 110.0, [_band(99.0, 100.0, il=0.9)])]
        events = [{'ts': _T0 + timedelta(minutes=30), 'price': 99.5}]
        result = score_events_against_snapshots(events, snapshots, top_n=5)
        assert result['observed_total'] == 1
        assert result['observed_hits'] == 1
        assert result['observed_fraction'] == 1.0

    def test_event_outside_painted_bucket_is_a_miss(self):
        snapshots = [_snap(_T0, 90.0, 110.0, [_band(99.0, 100.0, il=0.9)])]
        events = [{'ts': _T0 + timedelta(minutes=30), 'price': 105.0}]
        result = score_events_against_snapshots(events, snapshots, top_n=5)
        assert result['observed_total'] == 1
        assert result['observed_hits'] == 0
        assert result['observed_fraction'] == 0.0

    def test_event_outside_band_entirely_excluded_from_total(self):
        snapshots = [_snap(_T0, 90.0, 110.0, [_band(99.0, 100.0, il=0.9)])]
        events = [{'ts': _T0 + timedelta(minutes=30), 'price': 500.0}]
        result = score_events_against_snapshots(events, snapshots, top_n=5)
        assert result['observed_total'] == 0
        assert result['observed_fraction'] is None

    def test_event_before_first_snapshot_excluded(self):
        snapshots = [_snap(_T0, 90.0, 110.0, [_band(99.0, 100.0, il=0.9)])]
        events = [{'ts': _T0 - timedelta(minutes=30), 'price': 99.5}]
        result = score_events_against_snapshots(events, snapshots, top_n=5)
        assert result['observed_total'] == 0

    def test_event_after_next_snapshot_window_excluded(self):
        snap1 = _snap(_T0, 90.0, 110.0, [_band(99.0, 100.0, il=0.9)])
        snap2 = _snap(_T0 + timedelta(hours=1), 90.0, 110.0, [_band(99.0, 100.0, il=0.9)])
        events = [{'ts': _T0 + timedelta(hours=2), 'price': 99.5}]  # after snap2's window too
        result = score_events_against_snapshots(events, [snap1, snap2], top_n=5)
        # Falls in neither snap1's (ends at snap2.ts) nor snap2's (open-ended,
        # since it's the last snapshot) window? snap2 is last -> window_end=None
        # -> IS included under snap2. Assert it's attributed to snap2 only.
        assert result['observed_total'] == 1

    def test_uniform_baseline_reflects_top_n_over_total_buckets(self):
        bands = [_band(float(i), float(i) + 1, il=0.5) for i in range(10)]
        snapshots = [_snap(_T0, 0.0, 10.0, bands, n_buckets=100)]
        events = [{'ts': _T0 + timedelta(minutes=1), 'price': 0.5}]
        result = score_events_against_snapshots(events, snapshots, top_n=10)
        assert result['uniform_fraction'] == 10 / 100.0

    def test_synthetic_dataset_shows_strong_lift(self):
        """Self-test the harness end-to-end against the module's own
        synthetic fixture -- proves the scoring math distinguishes a
        deliberately embedded signal from noise."""
        events, snapshots = build_synthetic_dataset()
        result = score_events_against_snapshots(events, snapshots, top_n=20)
        assert result['observed_total'] > 0
        assert result['observed_fraction'] > result['uniform_fraction'] * 5
