"""Tests for carrac/facts/event_study.py — R3 own-history event stats.

All tests are deterministic and mock the DB cursor / data source — no real
DB is touched. Synthetic rows only (per the task's constraint: no live prod
access from the test environment).
"""
from __future__ import annotations

import datetime
from unittest.mock import MagicMock, patch


from carrac.facts.event_study import (
    _closest_close_on_or_after,
    _compute_event_next_day_stats,
    event_next_day_stats,
)


# ──────────────────────────────────────────────────────────────────────────
# _closest_close_on_or_after — PURE gap-tolerant lookup
# ──────────────────────────────────────────────────────────────────────────

class TestClosestCloseOnOrAfter:

    def test_exact_date_match(self):
        prices = {datetime.date(2026, 7, 1): 100.0}
        d, price = _closest_close_on_or_after(prices, datetime.date(2026, 7, 1))
        assert d == datetime.date(2026, 7, 1)
        assert price == 100.0

    def test_weekend_gap_finds_next_available_close(self):
        """Event lands on a Saturday (no close that day) — the next Monday's
        close (2 calendar days later) is used instead."""
        prices = {datetime.date(2026, 7, 6): 105.0}  # Monday
        d, price = _closest_close_on_or_after(prices, datetime.date(2026, 7, 4))  # Saturday
        assert d == datetime.date(2026, 7, 6)
        assert price == 105.0

    def test_gap_beyond_tolerance_returns_none(self):
        """Nothing within max_gap_days (default 4) forward -> (None, None),
        never a substituted/estimated price."""
        prices = {datetime.date(2026, 7, 10): 110.0}
        d, price = _closest_close_on_or_after(prices, datetime.date(2026, 7, 1))
        assert (d, price) == (None, None)

    def test_gap_at_exact_tolerance_boundary_is_found(self):
        """Exactly max_gap_days (4) forward is still within tolerance."""
        prices = {datetime.date(2026, 7, 5): 99.0}
        d, price = _closest_close_on_or_after(prices, datetime.date(2026, 7, 1), max_gap_days=4)
        assert d == datetime.date(2026, 7, 5)
        assert price == 99.0

    def test_never_looks_backward(self):
        """A close BEFORE `start` must never be picked, even if closer in
        calendar time than anything forward."""
        prices = {datetime.date(2026, 6, 30): 50.0, datetime.date(2026, 7, 3): 60.0}
        d, price = _closest_close_on_or_after(prices, datetime.date(2026, 7, 1))
        assert d == datetime.date(2026, 7, 3)
        assert price == 60.0


# ──────────────────────────────────────────────────────────────────────────
# _compute_event_next_day_stats — PURE
# ──────────────────────────────────────────────────────────────────────────

class TestComputeEventNextDayStats:

    def _prices(self, pairs):
        """pairs: list of (day_offset, close) from the fixture base date."""
        base = datetime.date(2026, 7, 1)
        return {base + datetime.timedelta(days=o): c for o, c in pairs}

    def test_below_min_n_returns_none(self):
        event_dates = [datetime.date(2026, 7, 1)]
        prices = self._prices([(0, 100.0), (1, 102.0)])
        assert _compute_event_next_day_stats(event_dates, prices, min_n=8) is None

    def test_exactly_min_n_returns_stats(self):
        """8 events, each with a clean 1%-move pair -> n=8, not gated out."""
        base = datetime.date(2026, 1, 1)
        event_dates = [base + datetime.timedelta(days=10 * i) for i in range(8)]
        prices = {}
        for i, ed in enumerate(event_dates):
            prices[ed] = 100.0
            prices[ed + datetime.timedelta(days=1)] = 101.0
        result = _compute_event_next_day_stats(event_dates, prices, min_n=8)
        assert result == {"n": 8, "median_abs_move_pct": 1.0}

    def test_median_of_mixed_moves(self):
        base = datetime.date(2026, 1, 1)
        # 3 events: +1%, -2%, +3% absolute moves -> median 2.0
        offsets_and_moves = [(0, 100.0, 101.0), (20, 100.0, 98.0), (40, 100.0, 103.0)]
        event_dates = []
        prices = {}
        for off, base_px, next_px in offsets_and_moves:
            ed = base + datetime.timedelta(days=off)
            event_dates.append(ed)
            prices[ed] = base_px
            prices[ed + datetime.timedelta(days=1)] = next_px
        result = _compute_event_next_day_stats(event_dates, prices, min_n=3)
        assert result["n"] == 3
        assert result["median_abs_move_pct"] == 2.0

    def test_move_is_absolute_value(self):
        """A -5% move reads as 5.0, never negative — 'absolute next-day move'."""
        # Reuse the same single pair 8x via distinct dates isn't needed —
        # test the sign handling directly via a 1-event, min_n=1 case.
        prices = {
            datetime.date(2026, 7, 1): 100.0,
            datetime.date(2026, 7, 2): 95.0,
        }
        result = _compute_event_next_day_stats([datetime.date(2026, 7, 1)], prices, min_n=1)
        assert result["median_abs_move_pct"] == 5.0

    def test_weekend_adjacent_event_uses_next_available_close(self):
        """Event ts falls on a Saturday; base close comes from the following
        Monday (gap-tolerant), next-day close from Tuesday."""
        event_dates = [datetime.date(2026, 7, 4)]  # Saturday
        prices = {
            datetime.date(2026, 7, 6): 100.0,  # Monday (base, +2d gap)
            datetime.date(2026, 7, 7): 102.0,  # Tuesday (next trading day)
        }
        result = _compute_event_next_day_stats(event_dates, prices, min_n=1)
        assert result == {"n": 1, "median_abs_move_pct": 2.0}

    def test_print_skipped_when_base_close_missing_beyond_gap(self):
        """No close at all within tolerance of the event date -> that print
        doesn't count toward n (not estimated, not defaulted)."""
        event_dates = [datetime.date(2026, 7, 1), datetime.date(2026, 8, 1)]
        prices = {
            # Only the second event has usable data.
            datetime.date(2026, 8, 1): 100.0,
            datetime.date(2026, 8, 2): 101.0,
        }
        result = _compute_event_next_day_stats(event_dates, prices, min_n=1)
        assert result == {"n": 1, "median_abs_move_pct": 1.0}

    def test_print_skipped_when_next_day_close_missing_beyond_gap(self):
        """Base close exists but nothing usable for the following day within
        tolerance -> that print is skipped, not estimated."""
        event_dates = [datetime.date(2026, 7, 1)]
        prices = {datetime.date(2026, 7, 1): 100.0}  # no next-day close at all
        assert _compute_event_next_day_stats(event_dates, prices, min_n=1) is None

    def test_zero_base_price_skipped_not_divided_by_zero(self):
        event_dates = [datetime.date(2026, 7, 1)]
        prices = {datetime.date(2026, 7, 1): 0.0, datetime.date(2026, 7, 2): 5.0}
        assert _compute_event_next_day_stats(event_dates, prices, min_n=1) is None

    def test_empty_event_dates_returns_none(self):
        assert _compute_event_next_day_stats([], {}, min_n=1) is None


# ──────────────────────────────────────────────────────────────────────────
# event_next_day_stats — DB reader wrapper
# ──────────────────────────────────────────────────────────────────────────

class TestEventNextDayStats:

    def _fake_conn(self, event_rows, price_rows):
        cursor = MagicMock()
        cursor.fetchall.side_effect = [event_rows, price_rows]
        conn = MagicMock()
        conn.cursor.return_value = cursor
        return conn

    def test_returns_none_for_unrecognized_asset(self):
        """No DB touched at all for an asset outside TARGET_ASSETS."""
        conn = MagicMock()
        result = event_next_day_stats("CPI", asset="NOT_A_REAL_ASSET", conn=conn)
        assert result is None
        conn.cursor.assert_not_called()

    def test_returns_stats_from_injected_conn(self):
        base = datetime.date(2026, 1, 1)
        event_rows = [{"ts": base + datetime.timedelta(days=10 * i)} for i in range(8)]
        price_rows = []
        for r in event_rows:
            ed = r["ts"]
            price_rows.append({"ts": ed, "close": 100.0})
            price_rows.append({"ts": ed + datetime.timedelta(days=1), "close": 101.0})
        conn = self._fake_conn(event_rows, price_rows)
        result = event_next_day_stats("CPI", asset="SPX", min_n=8, conn=conn)
        assert result == {"n": 8, "median_abs_move_pct": 1.0}

    def test_returns_none_below_gate(self):
        conn = self._fake_conn(
            event_rows=[{"ts": datetime.date(2026, 7, 1)}],
            price_rows=[{"ts": datetime.date(2026, 7, 1), "close": 100.0},
                        {"ts": datetime.date(2026, 7, 2), "close": 101.0}],
        )
        result = event_next_day_stats("CPI", asset="SPX", min_n=8, conn=conn)
        assert result is None

    def test_returns_none_when_no_event_rows(self):
        conn = self._fake_conn(event_rows=[], price_rows=[])
        assert event_next_day_stats("GDP", asset="SPX", conn=conn) is None

    def test_non_fatal_on_db_error_with_injected_conn(self):
        conn = MagicMock()
        conn.cursor.side_effect = RuntimeError("DB offline")
        assert event_next_day_stats("CPI", asset="SPX", conn=conn) is None

    def test_non_fatal_when_reader_raises(self):
        """conn=None path: get_quant_conn() itself raising is caught (non-fatal)."""
        with patch("carrac.facts.event_study.get_quant_conn", side_effect=Exception("quant DB down")):
            assert event_next_day_stats("CPI", asset="SPX") is None

    def test_lookback_window_passed_to_event_query(self):
        conn = self._fake_conn(event_rows=[], price_rows=[])
        today = datetime.date(2026, 7, 2)
        event_next_day_stats("CPI", asset="SPX", lookback_days=730, conn=conn, today=today)
        # First execute() call is the event-dates query.
        first_call_args = conn.cursor.return_value.execute.call_args_list[0]
        params = first_call_args[0][1]
        assert params[0] == "CPI"
        assert params[1] == today - datetime.timedelta(days=730)
        assert params[2] == today

    def test_price_query_uses_target_assets_mapping(self):
        """SPX resolves to (asset='SPX', source='yahoo') via
        quant.engine.config.TARGET_ASSETS — never guessed."""
        conn = self._fake_conn(event_rows=[], price_rows=[])
        event_next_day_stats("CPI", asset="SPX", conn=conn)
        second_call_args = conn.cursor.return_value.execute.call_args_list[1]
        params = second_call_args[0][1]
        assert params[0] == "SPX"
        assert params[1] == "yahoo"
