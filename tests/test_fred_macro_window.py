"""
Tests for the FRED macro collector's per-series daily lookback window.

Pins against the M2 quant series going stale/gappy in production
even though the collector never raised an exception and never missed a cron
run. Root cause: fred_macro's daily (non-backfill) mode used one fixed 30-day
trailing window for every series, but a series' FRED *publication* cadence can
be far coarser than its own *observation* cadence. WM2NS ('M2') is dated
weekly, yet FRED only publishes it via the H.6 release, which fires roughly
once a month and bundles 4-5 weeks of catch-up observations at once — with
the oldest observation in a batch sometimes 40-50+ days old by the day it is
first published. A 30-day window can never see those on release day (they are
already older than the window), which permanently drops them: no exception,
just fewer rows upserted than actually exist.

Observed on real data: quant.macro was silently missing several WM2NS weeks (H.6
publish lag reached 43-50 days while the window only looked back 30). FED_BALANCE (WALCL) and TGA (WTREGEN), also
tagged 'weekly', showed no such gaps — they are genuinely published weekly
(H.4.1 / Daily Treasury Statement), so this is an M2-specific mismatch, not a
generic weekly-series problem.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from quant.collectors.fred_macro import (
    DAILY_WINDOW_DAYS,
    DEFAULT_DAILY_WINDOW_DAYS,
    FRED_SERIES,
    FredMacroCollector,
)

# Worst publish lag actually observed for WM2NS in production (50 days). The default window
# must comfortably exceed this or the exact same gap reappears.
_WORST_OBSERVED_WM2NS_LAG_DAYS = 50


@pytest.fixture
def collector(monkeypatch):
    monkeypatch.setenv('FRED_API_KEY', 'test-key')
    # collect() best-effort calls into quant.collectors.event_calendar for
    # event-actuals backfill; those touch the DB and are irrelevant to the
    # windowing behaviour under test, so no-op them.
    with patch('quant.collectors.event_calendar.fill_event_actuals', return_value=0), \
         patch('quant.collectors.event_calendar.fetch_fred_release_events', return_value=0):
        yield FredMacroCollector(dry_run=True)


class TestWindowConstants:
    """Structural: the window table must exist and be safely sized."""

    def test_daily_series_get_30_day_window(self):
        assert DAILY_WINDOW_DAYS['daily'] == 30

    def test_default_window_covers_worst_observed_fred_lag(self):
        assert DEFAULT_DAILY_WINDOW_DAYS >= _WORST_OBSERVED_WM2NS_LAG_DAYS

    def test_m2_is_not_tagged_daily(self):
        """M2 must not fall into the narrow 'daily' bucket."""
        series_id, freq = FRED_SERIES['M2']
        assert series_id == 'WM2NS'
        assert freq != 'daily'

    def test_m2_frequency_resolves_to_default_wide_window(self):
        _, freq = FRED_SERIES['M2']
        assert DAILY_WINDOW_DAYS.get(freq, DEFAULT_DAILY_WINDOW_DAYS) == DEFAULT_DAILY_WINDOW_DAYS


class TestCollectWindowPerSeries:
    """collect() must pass a per-series window (by frequency) to _fetch_and_store."""

    def _run_collect_capturing_starts(self, collector):
        calls = {}

        def fake_fetch(metric_key, series_id, start, end):
            calls[metric_key] = start

        with patch.object(collector, '_fetch_and_store', side_effect=fake_fetch), \
             patch('time.sleep'):
            collector.collect()
        return calls

    def test_daily_series_uses_30_day_window(self, collector):
        calls = self._run_collect_capturing_starts(collector)

        now = datetime.now(timezone.utc)
        expected = (now - timedelta(days=30)).strftime('%Y-%m-%d')
        assert calls['FED_FUNDS'] == expected  # FED_FUNDS is tagged 'daily'

    def test_m2_uses_wide_window_not_30_days(self, collector):
        calls = self._run_collect_capturing_starts(collector)

        now = datetime.now(timezone.utc)
        expected_wide = (now - timedelta(days=DEFAULT_DAILY_WINDOW_DAYS)).strftime('%Y-%m-%d')
        stale_30d_window = (now - timedelta(days=30)).strftime('%Y-%m-%d')

        assert calls['M2'] == expected_wide
        assert calls['M2'] < stale_30d_window, (
            "M2's daily window must be wider than 30 days, "
            "otherwise gaps appear in the series."
        )

    def test_backfill_mode_uses_backfill_start_regardless_of_frequency(self, monkeypatch):
        monkeypatch.setenv('FRED_API_KEY', 'test-key')
        with patch('quant.collectors.event_calendar.fill_event_actuals', return_value=0), \
             patch('quant.collectors.event_calendar.fetch_fred_release_events', return_value=0):
            c = FredMacroCollector(dry_run=True, backfill=True, backfill_start='2020-01-01')

        calls = self._run_collect_capturing_starts(c)

        # Both a 'daily' series and the 'weekly'-tagged M2 must ignore the
        # per-frequency window table entirely in backfill mode.
        assert calls['FED_FUNDS'] == '2020-01-01'
        assert calls['M2'] == '2020-01-01'
