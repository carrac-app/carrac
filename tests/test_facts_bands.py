"""Tests for carrac/facts/bands.py — the editorial bands, plus the
Derivatives Desk (the first PAID-gated band).

Docket · Liquidity & Credit · Breadth internals · Regime clock.
Rotation (RRG) · Flows & Attention.
Derivatives Desk (BTC + ETH) — see build_desk's NO-LEAK RULE.

All tests are deterministic and mock the DB cursor / data source — no real
DB or Redis is touched. Bulletproof-rule assertions (no 'expected'/consensus
key anywhere) get their own explicit test even though the builder never
selects that column, as defense-in-depth.
"""
from __future__ import annotations

import datetime
from datetime import timezone
from unittest.mock import MagicMock, patch


from carrac.facts.bands import (
    build_breadth,
    build_desk,
    build_docket,
    build_flows,
    build_liquidity,
    build_regime_clock,
    build_rotation,
    _compute_regime_clock,
    _format_event_history,
    _is_desk_stale,
    _percentile_meaning,
    _shape_breadth,
    _shape_desk_asset,
    _shape_docket,
    _shape_flows,
    _shape_liquidity,
    _shape_rotation,
)


# ──────────────────────────────────────────────────────────────────────────
# Docket — _shape_docket (PURE) + build_docket (DB reader)
# ──────────────────────────────────────────────────────────────────────────

class TestShapeDocket:

    def test_chronological_ordering(self):
        """Out-of-order raw rows come back sorted ascending by 'when'."""
        rows = [
            {"ts": "2026-07-05", "title": "PPI", "impact": "medium", "actual": None},
            {"ts": "2026-07-01", "title": "FOMC", "impact": "high", "actual": None},
            {"ts": "2026-07-03", "title": "CPI", "impact": "high", "actual": "2.4% YoY"},
        ]
        out = _shape_docket(rows)
        assert [r["when"] for r in out] == ["2026-07-01", "2026-07-03", "2026-07-05"]
        assert [r["title"] for r in out] == ["FOMC", "CPI", "PPI"]

    def test_no_expected_key_anywhere(self):
        """Bulletproof rule: never carry a consensus/expected value."""
        rows = [{"ts": "2026-07-03", "title": "CPI", "impact": "high", "actual": None}]
        out = _shape_docket(rows)
        assert len(out) == 1
        assert "expected" not in out[0]
        assert "consensus" not in out[0]

    def test_history_always_reserved_none(self):
        rows = [{"ts": "2026-07-03", "title": "CPI", "impact": "high", "actual": "3.1%"}]
        out = _shape_docket(rows)
        assert out[0]["history"] is None

    def test_actual_none_before_release(self):
        rows = [{"ts": "2026-07-08", "title": "NFP", "impact": "high", "actual": None}]
        out = _shape_docket(rows)
        assert out[0]["actual"] is None

    def test_actual_filled_after_release(self):
        rows = [{"ts": "2026-06-30", "title": "CPI", "impact": "high", "actual": "2.4% YoY"}]
        out = _shape_docket(rows)
        assert out[0]["actual"] == "2.4% YoY"

    def test_impact_normalized_to_lowercase(self):
        """The impact-chip CSS class binding assumes lowercase — normalize
        whatever case the events table happens to store."""
        rows = [
            {"ts": "2026-07-01", "title": "FOMC", "impact": "HIGH", "actual": None},
            {"ts": "2026-07-02", "title": "ECB", "impact": "Medium", "actual": None},
        ]
        out = _shape_docket(rows)
        assert out[0]["impact"] == "high"
        assert out[1]["impact"] == "medium"

    def test_impact_none_stays_none(self):
        rows = [{"ts": "2026-07-01", "title": "Unknown event", "impact": None, "actual": None}]
        out = _shape_docket(rows)
        assert out[0]["impact"] is None

    def test_date_object_isoformat(self):
        """A real DB date object (not a string) formats via .isoformat()."""
        rows = [{"ts": datetime.date(2026, 7, 3), "title": "CPI", "impact": "high", "actual": None}]
        out = _shape_docket(rows)
        assert out[0]["when"] == "2026-07-03"

    def test_empty_rows_returns_empty_list(self):
        assert _shape_docket([]) == []


class TestBuildDocket:

    def _fake_conn(self, rows):
        cursor = MagicMock()
        cursor.fetchall.return_value = rows
        conn = MagicMock()
        conn.cursor.return_value = cursor
        return conn

    def test_window_is_yesterday_through_plus_7_days(self):
        """The SQL window bounds passed to the query are [today-1, today+7]."""
        conn = self._fake_conn([])
        today = datetime.date(2026, 7, 2)
        build_docket(conn=conn, today=today)
        args, kwargs = conn.cursor.return_value.execute.call_args
        params = args[1]
        assert params[0] == datetime.date(2026, 7, 1)
        assert params[1] == datetime.date(2026, 7, 9)

    def test_returns_none_when_no_rows(self):
        conn = self._fake_conn([])
        assert build_docket(conn=conn) is None

    def test_returns_shaped_rows(self):
        rows = [{"ts": "2026-07-03", "title": "CPI", "impact": "high", "actual": None}]
        conn = self._fake_conn(rows)
        result = build_docket(conn=conn)
        assert result == [{
            "when": "2026-07-03", "title": "CPI", "impact": "high",
            "actual": None, "history": None,
        }]

    def test_non_fatal_on_db_error_with_injected_conn(self):
        conn = MagicMock()
        conn.cursor.side_effect = RuntimeError("DB offline")
        assert build_docket(conn=conn) is None

    def test_non_fatal_when_reader_raises(self):
        """conn=None path: get_quant_conn() itself raising is caught (non-fatal)."""
        with patch("carrac.facts.bands.get_quant_conn", side_effect=Exception("quant DB down")):
            assert build_docket() is None


# ──────────────────────────────────────────────────────────────────────────
# _format_event_history — R3 exact contract string (PURE)
# ──────────────────────────────────────────────────────────────────────────

class TestFormatEventHistory:

    def test_exact_contract_string(self):
        stats = {"n": 14, "median_abs_move_pct": 1.1}
        assert _format_event_history("CPI", stats) == (
            "Last 14 CPI prints: SPX median absolute next-day move 1.1%"
        )

    def test_rounds_to_one_decimal_in_display(self):
        stats = {"n": 9, "median_abs_move_pct": 2.049}
        assert _format_event_history("NFP", stats) == (
            "Last 9 NFP prints: SPX median absolute next-day move 2.0%"
        )


# ──────────────────────────────────────────────────────────────────────────
# _shape_docket — R3 history enrichment via event_stats
# ──────────────────────────────────────────────────────────────────────────

class TestShapeDocketEventHistory:

    def test_history_filled_when_event_type_has_stats(self):
        rows = [{"ts": "2026-07-03", "event_type": "CPI", "title": "CPI (June)",
                 "impact": "high", "actual": "2.4% YoY"}]
        event_stats = {"CPI": {"n": 14, "median_abs_move_pct": 1.1}}
        out = _shape_docket(rows, event_stats)
        assert out[0]["history"] == "Last 14 CPI prints: SPX median absolute next-day move 1.1%"

    def test_history_none_when_event_type_not_in_stats(self):
        rows = [{"ts": "2026-07-03", "event_type": "FOMC", "title": "FOMC",
                 "impact": "high", "actual": None}]
        out = _shape_docket(rows, {"CPI": {"n": 14, "median_abs_move_pct": 1.1}})
        assert out[0]["history"] is None

    def test_history_none_when_event_stats_omitted(self):
        """Back-compat: callers that never pass event_stats (or pass None)
        always get history=None, same as before R3 wiring existed."""
        rows = [{"ts": "2026-07-03", "event_type": "CPI", "title": "CPI",
                 "impact": "high", "actual": None}]
        assert _shape_docket(rows)[0]["history"] is None
        assert _shape_docket(rows, None)[0]["history"] is None

    def test_event_type_missing_from_row_does_not_crash(self):
        """A row without an 'event_type' key (e.g. hand-built test fixtures)
        never raises — history simply stays None."""
        rows = [{"ts": "2026-07-03", "title": "CPI", "impact": "high", "actual": None}]
        out = _shape_docket(rows, {"CPI": {"n": 14, "median_abs_move_pct": 1.1}})
        assert out[0]["history"] is None

    def test_event_type_matched_case_insensitively(self):
        rows = [{"ts": "2026-07-03", "event_type": "cpi", "title": "CPI",
                 "impact": "high", "actual": None}]
        out = _shape_docket(rows, {"CPI": {"n": 14, "median_abs_move_pct": 1.1}})
        assert out[0]["history"] is not None

    def test_output_dict_never_carries_event_type_key(self):
        """The contract's docket row shape is when/title/impact/actual/history
        only — event_type is an internal lookup key, never surfaced."""
        rows = [{"ts": "2026-07-03", "event_type": "CPI", "title": "CPI",
                 "impact": "high", "actual": None}]
        out = _shape_docket(rows, {"CPI": {"n": 14, "median_abs_move_pct": 1.1}})
        assert set(out[0].keys()) == {"when", "title", "impact", "actual", "history"}


# ──────────────────────────────────────────────────────────────────────────
# build_docket — R3 event-stats gather wiring
# ──────────────────────────────────────────────────────────────────────────

class TestBuildDocketEventHistory:

    def _fake_conn(self, rows):
        cursor = MagicMock()
        cursor.fetchall.return_value = rows
        conn = MagicMock()
        conn.cursor.return_value = cursor
        return conn

    def test_history_filled_for_high_impact_type_present_in_window(self):
        rows = [{"ts": "2026-07-03", "event_type": "CPI", "title": "CPI (June)",
                 "impact": "high", "actual": None}]
        conn = self._fake_conn(rows)
        with patch("carrac.facts.bands.event_next_day_stats",
                   return_value={"n": 14, "median_abs_move_pct": 1.1}) as mock_stats:
            result = build_docket(conn=conn)
        mock_stats.assert_called_once_with("CPI")
        assert result[0]["history"] == "Last 14 CPI prints: SPX median absolute next-day move 1.1%"

    def test_history_none_when_stats_gate_fails(self):
        rows = [{"ts": "2026-07-03", "event_type": "CPI", "title": "CPI",
                 "impact": "high", "actual": None}]
        conn = self._fake_conn(rows)
        with patch("carrac.facts.bands.event_next_day_stats", return_value=None):
            result = build_docket(conn=conn)
        assert result[0]["history"] is None

    def test_event_stats_not_queried_for_non_high_impact_type(self):
        """ECB isn't in _HIGH_IMPACT_EVENT_TYPES — never looked up."""
        rows = [{"ts": "2026-07-03", "event_type": "ECB", "title": "ECB Rate Decision",
                 "impact": "medium", "actual": None}]
        conn = self._fake_conn(rows)
        with patch("carrac.facts.bands.event_next_day_stats") as mock_stats:
            result = build_docket(conn=conn)
        mock_stats.assert_not_called()
        assert result[0]["history"] is None

    def test_event_stats_queried_once_per_distinct_type(self):
        """Two CPI rows + one FOMC row -> exactly 2 calls (CPI, FOMC), not 3."""
        rows = [
            {"ts": "2026-07-03", "event_type": "CPI", "title": "CPI", "impact": "high", "actual": None},
            {"ts": "2026-07-10", "event_type": "CPI", "title": "CPI (next)", "impact": "high", "actual": None},
            {"ts": "2026-07-05", "event_type": "FOMC", "title": "FOMC", "impact": "high", "actual": None},
        ]
        conn = self._fake_conn(rows)
        with patch("carrac.facts.bands.event_next_day_stats", return_value=None) as mock_stats:
            build_docket(conn=conn)
        assert mock_stats.call_count == 2
        called_types = {c.args[0] for c in mock_stats.call_args_list}
        assert called_types == {"CPI", "FOMC"}

    def test_no_event_stats_lookup_when_window_empty(self):
        conn = self._fake_conn([])
        with patch("carrac.facts.bands.event_next_day_stats") as mock_stats:
            assert build_docket(conn=conn) is None
        mock_stats.assert_not_called()


# ──────────────────────────────────────────────────────────────────────────
# _percentile_meaning — shared bottom/top phrasing helper (R1 + R2)
# ──────────────────────────────────────────────────────────────────────────

class TestPercentileMeaning:

    def test_top_phrasing_above_50(self):
        assert _percentile_meaning(92.0) == "in the top 8% of the last 90 days"

    def test_bottom_phrasing_at_or_below_50(self):
        assert _percentile_meaning(8.0) == "in the bottom 8% of the last 90 days"

    def test_exactly_50_reads_as_bottom(self):
        assert _percentile_meaning(50.0) == "in the bottom 50% of the last 90 days"

    def test_clamped_to_at_least_1_percent(self):
        assert _percentile_meaning(99.7) == "in the top 1% of the last 90 days"
        assert _percentile_meaning(0.2) == "in the bottom 1% of the last 90 days"


# ──────────────────────────────────────────────────────────────────────────
# Liquidity — _shape_liquidity (PURE) + build_liquidity (DB reader)
# ──────────────────────────────────────────────────────────────────────────

class TestShapeLiquidity:

    def test_skips_missing_series(self):
        """A curated metric absent from macro_rows is skipped, not invented."""
        macro_rows = {"VIX": 16.5, "HY_SPREAD": 3.12}  # TGA, M2, RRP, claims, breakeven missing
        zscore_rows = {}
        out = _shape_liquidity(macro_rows, zscore_rows)
        labels = [r["label"] for r in out]
        assert labels == ["VIX", "High-yield spread"]

    def test_fixed_order_regardless_of_input_order(self):
        macro_rows = {"BREAKEVEN_10Y": 2.3, "VIX": 14.0, "M2": 21500.0}
        out = _shape_liquidity(macro_rows, {})
        assert [r["label"] for r in out] == ["VIX", "M2 money supply", "10Y inflation breakeven"]

    def test_zscore_and_percentile_attached_when_present(self):
        macro_rows = {"VIX": 22.0}
        zscore_rows = {"VIX": (1.8, 92.0)}
        out = _shape_liquidity(macro_rows, zscore_rows)
        assert out[0]["zscore"] == 1.8
        assert out[0]["percentile"] == 92.0
        assert out[0]["meaning"] == "in the top 8% of the last 90 days"

    def test_bottom_phrasing_for_low_percentile(self):
        # p8 = the value exceeds only 8% of the window -> "bottom 8%", never
        # the inverted "lower than 8%".
        macro_rows = {"VIX": 10.0}
        zscore_rows = {"VIX": (-1.5, 8.0)}
        out = _shape_liquidity(macro_rows, zscore_rows)
        assert out[0]["meaning"] == "in the bottom 8% of the last 90 days"

    def test_zscore_none_when_not_in_zscore_rows(self):
        macro_rows = {"VIX": 22.0}
        out = _shape_liquidity(macro_rows, {})
        assert out[0]["zscore"] is None
        assert out[0]["percentile"] is None
        assert out[0]["meaning"] is None

    def test_value_formatting_per_metric(self):
        macro_rows = {
            "VIX": 16.53,
            "HY_SPREAD": 3.124,
            "REVERSE_REPO": 215.0,
            "TGA": 700.0,
            "M2": 21500.0,
            "INITIAL_CLAIMS": 220000.0,
            "BREAKEVEN_10Y": 2.34,
        }
        out = _shape_liquidity(macro_rows, {})
        values = {r["label"]: r["value"] for r in out}
        assert values["VIX"] == "16.5"
        assert values["High-yield spread"] == "3.12%"
        assert values["Reverse repo (Fed)"] == "$215B"
        assert values["Treasury General Account"] == "$700B"
        assert values["M2 money supply"] == "$21.5T"
        assert values["Initial jobless claims"] == "220K"
        assert values["10Y inflation breakeven"] == "2.34%"

    def test_all_missing_returns_empty_list(self):
        assert _shape_liquidity({}, {}) == []

    def test_formatter_exception_skips_row_non_fatal(self):
        """A raw value that can't be formatted (e.g. non-numeric) is skipped,
        not raised."""
        macro_rows = {"VIX": "not-a-number"}
        out = _shape_liquidity(macro_rows, {})
        assert out == []


class TestBuildLiquidity:

    def _fake_conn(self, macro_fetchall, zscore_fetchall):
        cursor = MagicMock()
        cursor.fetchall.side_effect = [macro_fetchall, zscore_fetchall]
        conn = MagicMock()
        conn.cursor.return_value = cursor
        return conn

    def test_returns_shaped_rows(self):
        conn = self._fake_conn(
            macro_fetchall=[{"metric": "VIX", "value": 16.5}],
            zscore_fetchall=[{"metric": "VIX", "zscore": 1.2, "percentile": 80.0}],
        )
        result = build_liquidity(conn=conn)
        assert result == [{
            "label": "VIX", "value": "16.5", "zscore": 1.2,
            "percentile": 80.0, "meaning": "in the top 20% of the last 90 days",
        }]

    def test_returns_none_when_all_series_missing(self):
        conn = self._fake_conn(macro_fetchall=[], zscore_fetchall=[])
        assert build_liquidity(conn=conn) is None

    def test_non_fatal_on_db_error_with_injected_conn(self):
        conn = MagicMock()
        conn.cursor.side_effect = RuntimeError("DB offline")
        assert build_liquidity(conn=conn) is None

    def test_non_fatal_when_reader_raises(self):
        with patch("carrac.facts.bands.get_quant_conn", side_effect=Exception("quant DB down")):
            assert build_liquidity() is None


# ──────────────────────────────────────────────────────────────────────────
# Breadth — _shape_breadth (PURE) + build_breadth (cache reader)
# ──────────────────────────────────────────────────────────────────────────

class TestShapeBreadth:

    def _base(self, **overrides):
        data = {
            "covered": 250,
            "above_sma200": {"count": 155, "pct": 62.0},
            "above_sma50": {"count": 100, "pct": 40.0},
            "oversold": {"count": 10, "pct": 4.0},
            "overbought": {"count": 20, "pct": 8.0},
            "new_20d_highs": {"count": 12, "pct": 4.8},
            "new_20d_lows": {"count": 3, "pct": 1.2},
            "advancers": 140,
            "decliners": 90,
        }
        data.update(overrides)
        return data

    def test_above_200_warm_when_majority(self):
        out = _shape_breadth(self._base())
        row = next(r for r in out if r["label"] == "Above 200-day")
        assert row["value"] == "62%"
        assert row["tone"] == "warm"

    def test_above_200_cold_when_minority(self):
        out = _shape_breadth(self._base(above_sma200={"count": 50, "pct": 20.0}))
        row = next(r for r in out if r["label"] == "Above 200-day")
        assert row["tone"] == "cold"

    def test_above_50_tone_mapping(self):
        out = _shape_breadth(self._base(above_sma50={"count": 200, "pct": 80.0}))
        row = next(r for r in out if r["label"] == "Above 50-day")
        assert row["tone"] == "warm"

    def test_oversold_cold_above_threshold(self):
        out = _shape_breadth(self._base(oversold={"count": 60, "pct": 25.0}))
        row = next(r for r in out if r["label"] == "Oversold (RSI < 30)")
        assert row["tone"] == "cold"

    def test_oversold_none_below_threshold(self):
        out = _shape_breadth(self._base(oversold={"count": 5, "pct": 2.0}))
        row = next(r for r in out if r["label"] == "Oversold (RSI < 30)")
        assert row["tone"] is None

    def test_overbought_warm_above_threshold(self):
        out = _shape_breadth(self._base(overbought={"count": 80, "pct": 32.0}))
        row = next(r for r in out if r["label"] == "Overbought (RSI > 70)")
        assert row["tone"] == "warm"

    def test_highs_vs_lows_tone(self):
        out = _shape_breadth(self._base())
        row = next(r for r in out if r["label"] == "New 20-day highs vs lows")
        assert row["value"] == "12 / 3"
        assert row["tone"] == "warm"

    def test_advancers_vs_decliners_tone(self):
        out = _shape_breadth(self._base(advancers=50, decliners=120))
        row = next(r for r in out if r["label"] == "Advancers vs decliners")
        assert row["value"] == "50 / 120"
        assert row["tone"] == "cold"

    def test_uncovered_universe_returns_empty_list(self):
        assert _shape_breadth({"covered": 0}) == []

    def test_none_data_returns_empty_list(self):
        assert _shape_breadth(None) == []

    def test_missing_section_is_skipped_not_invented(self):
        data = self._base()
        del data["above_sma200"]
        out = _shape_breadth(data)
        assert "Above 200-day" not in [r["label"] for r in out]


class TestBuildBreadth:

    def test_injected_data_shapes_correctly(self):
        data = {
            "covered": 100,
            "above_sma200": {"count": 60, "pct": 60.0},
        }
        result = build_breadth(breadth_data=data)
        assert result == [{"label": "Above 200-day", "value": "60%", "tone": "warm"}]

    def test_returns_none_when_uncovered(self):
        assert build_breadth(breadth_data={"covered": 0}) is None

    def test_non_fatal_when_reader_raises(self):
        with patch("carrac.facts.bands.get_breadth", side_effect=Exception("cache down")):
            assert build_breadth() is None


# ──────────────────────────────────────────────────────────────────────────
# Regime clock — _compute_regime_clock (PURE) + build_regime_clock (reader)
# ──────────────────────────────────────────────────────────────────────────

class TestComputeRegimeClock:

    def _days(self, spec):
        """spec: list of (regime, n_days) → expands to day rows."""
        out = []
        i = 0
        for regime, n in spec:
            for _ in range(n):
                out.append({"date": f"day{i}", "regime": regime})
                i += 1
        return out

    def test_median_math_with_enough_completed_runs(self):
        # Completed runs: fav(10), unfav(20), fav(30) → median 20. Current: unfav(5).
        day_rows = self._days([
            ("favorable", 10), ("unfavorable", 20), ("favorable", 30), ("unfavorable", 5),
        ])
        result = _compute_regime_clock(day_rows)
        assert result["state"] == "unfavorable"
        assert result["day_in_state"] == 5
        assert result["median_run_days"] == 20
        assert result["note"] == "shorter than the median run"

    def test_longer_than_median_note(self):
        day_rows = self._days([
            ("favorable", 10), ("unfavorable", 20), ("favorable", 15), ("unfavorable", 79),
        ])
        result = _compute_regime_clock(day_rows)
        assert result["median_run_days"] == 15
        assert result["day_in_state"] == 79
        assert result["note"] == "longer than the median run"

    def test_fewer_than_3_completed_runs_gives_none(self):
        """Only 2 completed runs before the current one → median_run_days=None, note=None."""
        day_rows = self._days([("favorable", 10), ("unfavorable", 20), ("favorable", 5)])
        result = _compute_regime_clock(day_rows)
        assert result["state"] == "favorable"
        assert result["day_in_state"] == 5
        assert result["median_run_days"] is None
        assert result["note"] is None

    def test_zero_completed_runs_single_run_ever(self):
        day_rows = self._days([("favorable", 1)])
        result = _compute_regime_clock(day_rows)
        assert result["state"] == "favorable"
        assert result["day_in_state"] == 1
        assert result["median_run_days"] is None
        assert result["note"] is None

    def test_empty_input_returns_none(self):
        assert _compute_regime_clock([]) is None


class TestBuildRegimeClock:

    def _fake_conn(self, rows):
        cursor = MagicMock()
        cursor.fetchall.return_value = rows
        conn = MagicMock()
        conn.cursor.return_value = cursor
        return conn

    def test_reads_metadata_label_and_reverses_to_chronological(self):
        # DB returns newest-first (ts DESC) — builder must reverse to compute runs correctly.
        rows = [
            {"ts": datetime.date(2026, 7, 2), "metadata": {"label": "favorable"}},
            {"ts": datetime.date(2026, 7, 1), "metadata": {"label": "unfavorable"}},
        ]
        conn = self._fake_conn(rows)
        result = build_regime_clock(conn=conn)
        assert result["state"] == "favorable"
        assert result["day_in_state"] == 1

    def test_metadata_as_json_string_is_parsed(self):
        import json
        rows = [{"ts": datetime.date(2026, 7, 2), "metadata": json.dumps({"label": "favorable"})}]
        conn = self._fake_conn(rows)
        result = build_regime_clock(conn=conn)
        assert result["state"] == "favorable"

    def test_returns_none_when_no_rows(self):
        conn = self._fake_conn([])
        assert build_regime_clock(conn=conn) is None

    def test_non_fatal_on_db_error_with_injected_conn(self):
        conn = MagicMock()
        conn.cursor.side_effect = RuntimeError("DB offline")
        assert build_regime_clock(conn=conn) is None

    def test_non_fatal_when_reader_raises(self):
        with patch("carrac.facts.bands.get_quant_conn", side_effect=Exception("quant DB down")):
            assert build_regime_clock() is None

    def test_uses_bounded_limit_param(self):
        """The LIMIT is passed as a bound parameter, not string-interpolated."""
        conn = self._fake_conn([])
        build_regime_clock(conn=conn)
        args, kwargs = conn.cursor.return_value.execute.call_args
        assert args[1] == (730,)


# ──────────────────────────────────────────────────────────────────────────
# Rotation (R2) — _shape_rotation (PURE) + build_rotation (cache reader)
# ──────────────────────────────────────────────────────────────────────────

class TestShapeRotation:

    def _row(self, symbol, quadrant):
        return {"symbol": symbol, "quadrant": quadrant, "rs_score": 0, "rs_momentum": 0}

    def test_groups_by_quadrant_in_fixed_order(self):
        rows = [
            self._row("BTC", "lagging"),
            self._row("GOLD", "leading"),
            self._row("SPX", "improving"),
            self._row("DXY", "weakening"),
        ]
        out = _shape_rotation(rows)
        assert [r["quadrant"] for r in out] == ["Leading", "Improving", "Weakening", "Lagging"]

    def test_empty_quadrant_is_omitted(self):
        """Only 'leading' has assets — the other 3 quadrants are omitted, not
        rendered as empty shells."""
        rows = [self._row("BTC", "leading")]
        out = _shape_rotation(rows)
        assert len(out) == 1
        assert out[0]["quadrant"] == "Leading"
        assert out[0]["assets"] == [{"ticker": "BTC", "name": "Bitcoin"}]

    def test_within_quadrant_order_preserved_from_cache(self):
        """Cache is already rs_score-descending; the shaper must not re-sort."""
        rows = [
            self._row("ETH", "leading"),
            self._row("BTC", "leading"),
            self._row("GOLD", "leading"),
        ]
        out = _shape_rotation(rows)
        assert [a["ticker"] for a in out[0]["assets"]] == ["ETH", "BTC", "GOLD"]

    def test_unrecognized_quadrant_is_skipped(self):
        rows = [self._row("BTC", "sideways"), self._row("ETH", "leading")]
        out = _shape_rotation(rows)
        assert len(out) == 1
        assert [a["ticker"] for a in out[0]["assets"]] == ["ETH"]

    def test_quadrant_case_insensitive(self):
        rows = [self._row("BTC", "LEADING")]
        out = _shape_rotation(rows)
        assert out[0]["quadrant"] == "Leading"

    def test_missing_symbol_is_skipped(self):
        rows = [{"symbol": None, "quadrant": "leading"}, self._row("BTC", "leading")]
        out = _shape_rotation(rows)
        assert [a["ticker"] for a in out[0]["assets"]] == ["BTC"]

    def test_name_uses_asset_name_lookup(self):
        """A ticker outside the friendly-name map falls back to itself."""
        rows = [self._row("TSLA", "leading")]
        out = _shape_rotation(rows)
        assert out[0]["assets"][0] == {"ticker": "TSLA", "name": "TSLA"}

    def test_empty_input_returns_empty_list(self):
        assert _shape_rotation([]) == []


class TestBuildRotation:

    def test_returns_shaped_rows_from_injected_data(self):
        data = [{"symbol": "BTC", "quadrant": "leading"}]
        result = build_rotation(rs_data=data)
        assert result == [{"quadrant": "Leading", "assets": [{"ticker": "BTC", "name": "Bitcoin"}]}]

    def test_returns_none_when_cache_is_none(self):
        """A cold/unwarmed cache (get_cached_rs() -> None) is NOT recomputed —
        the band is simply omitted this render."""
        assert build_rotation(rs_data=None) is None

    def test_uses_get_cached_rs_when_no_data_injected(self):
        with patch("carrac.facts.bands.get_cached_rs", return_value=[{"symbol": "BTC", "quadrant": "leading"}]):
            result = build_rotation()
        assert result == [{"quadrant": "Leading", "assets": [{"ticker": "BTC", "name": "Bitcoin"}]}]

    def test_returns_none_when_cache_empty_list(self):
        assert build_rotation(rs_data=[]) is None

    def test_returns_none_when_all_quadrants_unrecognized(self):
        data = [{"symbol": "BTC", "quadrant": "unknown"}]
        assert build_rotation(rs_data=data) is None

    def test_non_fatal_when_reader_raises(self):
        with patch("carrac.facts.bands.get_cached_rs", side_effect=Exception("redis down")):
            assert build_rotation() is None

    def test_never_recomputes_rs_rankings(self):
        """Bulletproof rule for this band: build_rotation must read the cache
        only — it must never import/call _compute_rs_rankings() itself."""
        with patch("carrac.facts.bands.get_cached_rs", return_value=None) as mock_get:
            build_rotation()
            mock_get.assert_called_once()


# ──────────────────────────────────────────────────────────────────────────
# Flows & Attention (R2) — _shape_flows (PURE) + build_flows (DB reader)
# ──────────────────────────────────────────────────────────────────────────

class TestShapeFlows:

    def _base(self, **overrides):
        data = {
            "etf_recent": [142.0, 50.0, -30.0, 20.0, 10.0],
            "etf_zscore": (0.9, 71.0),
            "stable_raw": {"STABLECOIN_DOMINANCE": 6.8, "STABLECOIN_USDT_SUPPLY": 1.2e11},
            "stable_z": {"STABLECOIN_DOMINANCE": (0.5, 65.0), "STABLECOIN_USDT_SUPPLY": (1.1, 82.0)},
            "gtrend_raw": {"GTREND_BUY_BITCOIN": 62.0, "GTREND_CRYPTO": 30.0},
            "gtrend_z": {"GTREND_BUY_BITCOIN": (2.1, 96.0), "GTREND_CRYPTO": (0.3, 58.0)},
        }
        data.update(overrides)
        return data

    def test_etf_day_row(self):
        out = _shape_flows(self._base())
        row = next(r for r in out if r["label"] == "BTC ETF net flow (day)")
        assert row["value"] == "+$142M"
        assert row["zscore"] == 0.9
        assert row["percentile"] == 71.0
        assert row["meaning"] == "in the top 29% of the last 90 days"

    def test_etf_negative_day_row(self):
        out = _shape_flows(self._base(etf_recent=[-85.5]))
        row = next(r for r in out if r["label"] == "BTC ETF net flow (day)")
        assert row["value"] == "-$86M"

    def test_etf_5day_sum_row(self):
        out = _shape_flows(self._base())
        row = next(r for r in out if r["label"] == "BTC ETF net flow (5-day)")
        assert row["value"] == "+$192M"  # 142 + 50 - 30 + 20 + 10
        assert row["zscore"] is None
        assert row["percentile"] is None
        assert row["meaning"] is None

    def test_etf_rows_skipped_when_no_data(self):
        out = _shape_flows(self._base(etf_recent=[]))
        assert "BTC ETF net flow (day)" not in [r["label"] for r in out]
        assert "BTC ETF net flow (5-day)" not in [r["label"] for r in out]

    def test_etf_zscore_none_when_missing(self):
        out = _shape_flows(self._base(etf_zscore=None))
        row = next(r for r in out if r["label"] == "BTC ETF net flow (day)")
        assert row["zscore"] is None
        assert row["percentile"] is None
        assert row["meaning"] is None

    def test_stablecoin_dominance_row(self):
        out = _shape_flows(self._base())
        row = next(r for r in out if r["label"] == "Stablecoin dominance")
        assert row["value"] == "6.8%"
        assert row["zscore"] == 0.5
        assert row["percentile"] == 65.0
        assert row["meaning"] == "in the top 35% of the last 90 days"

    def test_usdt_supply_row_formatted_as_billions(self):
        out = _shape_flows(self._base())
        row = next(r for r in out if r["label"] == "USDT supply")
        assert row["value"] == "$120.0B"
        assert row["zscore"] == 1.1
        assert row["percentile"] == 82.0

    def test_stablecoin_rows_skipped_when_missing(self):
        out = _shape_flows(self._base(stable_raw={}))
        labels = [r["label"] for r in out]
        assert "Stablecoin dominance" not in labels
        assert "USDT supply" not in labels

    def test_top_abs_zscore_gtrend_row_wins(self):
        """GTREND_BUY_BITCOIN (|z|=2.1) beats GTREND_CRYPTO (|z|=0.3)."""
        out = _shape_flows(self._base())
        gtrend_rows = [r for r in out if r["label"] not in (
            "BTC ETF net flow (day)", "BTC ETF net flow (5-day)",
            "Stablecoin dominance", "USDT supply",
        )]
        assert len(gtrend_rows) == 1
        row = gtrend_rows[0]
        assert row["label"] == "'Buy BTC' search trend"
        assert row["value"] == "62"
        assert row["zscore"] == 2.1
        assert row["percentile"] == 96.0
        assert row["meaning"] == "in the top 4% of the last 90 days"

    def test_top_abs_zscore_picks_negative_extreme(self):
        """A strongly negative z-score beats a weaker positive one on |z|."""
        out = _shape_flows(self._base(gtrend_z={
            "GTREND_BUY_BITCOIN": (-3.0, 2.0),
            "GTREND_CRYPTO": (0.3, 58.0),
        }))
        gtrend_rows = [r for r in out if r["label"] not in (
            "BTC ETF net flow (day)", "BTC ETF net flow (5-day)",
            "Stablecoin dominance", "USDT supply",
        )]
        assert len(gtrend_rows) == 1
        assert gtrend_rows[0]["label"] == "'Buy BTC' search trend"
        assert gtrend_rows[0]["zscore"] == -3.0

    def test_gtrend_row_omitted_when_no_zscores(self):
        out = _shape_flows(self._base(gtrend_z={}))
        labels = [r["label"] for r in out]
        assert "'Buy BTC' search trend" not in labels
        assert "Crypto search trend" not in labels

    def test_all_missing_returns_empty_list(self):
        assert _shape_flows({}) == []

    def test_malformed_etf_value_skips_day_row_not_fatal(self):
        out = _shape_flows(self._base(etf_recent=["not-a-number"]))
        assert "BTC ETF net flow (day)" not in [r["label"] for r in out]


class TestBuildFlows:

    def _fake_conn(self, results):
        """``results``: list of fetchall/fetchone return values, in the exact
        call order _fetch_flows_rows issues them (etf_recent, etf_zscore-row,
        stable_raw, stable_z, gtrend_raw, gtrend_z)."""
        cursor = MagicMock()
        cursor.fetchall.side_effect = [results[0], results[2], results[3], results[4], results[5]]
        cursor.fetchone.return_value = results[1]
        conn = MagicMock()
        conn.cursor.return_value = cursor
        return conn

    def test_returns_shaped_rows(self):
        conn = self._fake_conn([
            [{"value": 142.0}],
            {"zscore": 0.9, "percentile": 71.0},
            [{"metric": "STABLECOIN_DOMINANCE", "value": 6.8}],
            [{"metric": "STABLECOIN_DOMINANCE", "zscore": 0.5, "percentile": 65.0}],
            [],
            [],
        ])
        result = build_flows(conn=conn)
        assert result is not None
        row = next(r for r in result if r["label"] == "BTC ETF net flow (day)")
        assert row["value"] == "+$142M"

    def test_returns_none_when_all_series_missing(self):
        conn = self._fake_conn([[], None, [], [], [], []])
        assert build_flows(conn=conn) is None

    def test_non_fatal_on_db_error_with_injected_conn(self):
        conn = MagicMock()
        conn.cursor.side_effect = RuntimeError("DB offline")
        assert build_flows(conn=conn) is None

    def test_non_fatal_when_reader_raises(self):
        with patch("carrac.facts.bands.get_quant_conn", side_effect=Exception("quant DB down")):
            assert build_flows() is None


# ──────────────────────────────────────────────────────────────────────────
# Derivatives Desk — the first PAID-gated band.
#
# COMPLIANCE: Deribit's ToS bars
# publishing its market/derived data, so every deribit_options.py column
# (dvol, iv_7d/30d/90d, term_structure_slope, skew_25d, iv_rv_premium,
# basis_pct, gex_net, gex_flip_price, options_max_pain,
# options_put_call_ratio) is excluded from the desk's row set. The desk is
# exactly 3 rows, Binance-only (quant/collectors/binance_daily.py —
# funding_rate_avg, oi_usd, long_short_ratio — licence-permitted).
#
# Hand-computed formatting expectations: funding_rate_avg 0.00008001 ->
# "+0.8 bp" (value * 1e4, one decimal, signed); oi_usd 6196224048.8778 ->
# "$6.20B"; long_short_ratio 1.8466 -> "1.85".
# ──────────────────────────────────────────────────────────────────────────

def _btc_row(**overrides):
    row = {
        "ts": datetime.date(2026, 7, 2),
        "funding_rate_avg": 0.00008001,
        "oi_usd": 6196224048.8778,
        "long_short_ratio": 1.8466,
    }
    row.update(overrides)
    return row


def _eth_row(**overrides):
    row = {
        "ts": datetime.date(2026, 7, 2),
        "funding_rate_avg": -0.00003512,
        "oi_usd": 4561234567.0,
        "long_short_ratio": 0.8734,
    }
    row.update(overrides)
    return row


_BTC_EXPECTED_UNLOCKED = {
    "Funding rate": "+0.8 bp",
    "Futures OI": "$6.20B",
    "Long/short ratio": "1.85",
}


class TestShapeDeskAsset:

    def test_unlocked_full_values_in_fixed_order(self):
        asset = _shape_desk_asset("BTC", _btc_row(), unlocked=True)
        assert asset["ticker"] == "BTC"
        assert asset["name"] == "Bitcoin"
        labels = [r["label"] for r in asset["rows"]]
        assert labels == list(_BTC_EXPECTED_UNLOCKED.keys())
        for row in asset["rows"]:
            assert row["value"] == _BTC_EXPECTED_UNLOCKED[row["label"]]

    def test_unlocked_negative_eth_values(self):
        asset = _shape_desk_asset("ETH", _eth_row(), unlocked=True)
        values = {r["label"]: r["value"] for r in asset["rows"]}
        assert values["Funding rate"] == "-0.4 bp"
        assert values["Futures OI"] == "$4.56B"
        assert values["Long/short ratio"] == "0.87"

    def test_free_row_marked_correctly(self):
        asset = _shape_desk_asset("BTC", _btc_row(), unlocked=True)
        free_flags = {r["label"]: r["free"] for r in asset["rows"]}
        assert free_flags["Funding rate"] is True
        assert all(v is False for k, v in free_flags.items() if k != "Funding rate")

    def test_locked_redacts_every_row_except_free(self):
        """NO-LEAK RULE: build_desk(row, unlocked=False) leaves a value ONLY
        on the free (Funding rate) row — every other resolved row keeps its
        label/hint but its value is None."""
        asset = _shape_desk_asset("BTC", _btc_row(), unlocked=False)
        values = {r["label"]: r["value"] for r in asset["rows"]}
        assert values["Funding rate"] == "+0.8 bp"
        for label, value in values.items():
            if label != "Funding rate":
                assert value is None, f"{label} leaked a real value while locked"
        # The row set itself is unchanged by lock state — same labels appear.
        assert set(values.keys()) == set(_BTC_EXPECTED_UNLOCKED.keys())

    def test_locked_leaves_exactly_one_valued_row(self):
        """Redaction test (3-row Binance-only set): a locked render has
        exactly one row with a real value — the Funding rate teaser."""
        asset = _shape_desk_asset("BTC", _btc_row(), unlocked=False)
        valued = [r for r in asset["rows"] if r["value"] is not None]
        assert len(valued) == 1
        assert valued[0]["label"] == "Funding rate"

    def test_locked_row_still_carries_hint_and_free_flag(self):
        asset = _shape_desk_asset("BTC", _btc_row(), unlocked=False)
        oi_row = next(r for r in asset["rows"] if r["label"] == "Futures OI")
        assert oi_row["value"] is None
        assert oi_row["free"] is False
        assert oi_row["hint"] == "open interest in USD (Binance)"

    def test_null_column_skips_row_entirely_even_when_unlocked(self):
        """Missing data is SKIPPED (no row at all) — distinct from redaction
        (row present, value None)."""
        asset = _shape_desk_asset("BTC", _btc_row(oi_usd=None), unlocked=True)
        labels = [r["label"] for r in asset["rows"]]
        assert "Futures OI" not in labels

    def test_null_column_skips_row_entirely_when_locked(self):
        asset = _shape_desk_asset("BTC", _btc_row(oi_usd=None), unlocked=False)
        labels = [r["label"] for r in asset["rows"]]
        assert "Futures OI" not in labels

    def test_unparseable_column_skips_row_non_fatal(self):
        asset = _shape_desk_asset("BTC", _btc_row(long_short_ratio="not-a-number"), unlocked=True)
        labels = [r["label"] for r in asset["rows"]]
        assert "Long/short ratio" not in labels

    def test_all_columns_missing_returns_none(self):
        row = {"ts": datetime.date(2026, 7, 2)}
        assert _shape_desk_asset("BTC", row, unlocked=True) is None

    def test_asset_name_lookup(self):
        asset = _shape_desk_asset("ETH", _eth_row(), unlocked=True)
        assert asset["name"] == "Ethereum"


class TestIsDeskStale:

    def test_today_date_is_fresh(self):
        now = datetime.datetime(2026, 7, 2, 15, 0, tzinfo=timezone.utc)
        assert _is_desk_stale(datetime.date(2026, 7, 2), now=now) is False

    def test_yesterday_within_48h_is_fresh(self):
        now = datetime.datetime(2026, 7, 2, 15, 0, tzinfo=timezone.utc)
        assert _is_desk_stale(datetime.date(2026, 7, 1), now=now) is False

    def test_two_days_ago_is_stale(self):
        """Midnight-of-2-days-ago to now is always >48h (48h-72h range)."""
        now = datetime.datetime(2026, 7, 2, 15, 0, tzinfo=timezone.utc)
        assert _is_desk_stale(datetime.date(2026, 6, 30), now=now) is True

    def test_far_past_is_stale(self):
        now = datetime.datetime(2026, 7, 2, 15, 0, tzinfo=timezone.utc)
        assert _is_desk_stale(datetime.date(2026, 6, 20), now=now) is True

    def test_none_is_stale(self):
        assert _is_desk_stale(None) is True

    def test_naive_datetime_treated_as_utc(self):
        now = datetime.datetime(2026, 7, 2, 15, 0, tzinfo=timezone.utc)
        ts = datetime.datetime(2026, 7, 2, 10, 0)  # naive, same day
        assert _is_desk_stale(ts, now=now) is False

    def test_tz_aware_datetime_respected(self):
        now = datetime.datetime(2026, 7, 2, 15, 0, tzinfo=timezone.utc)
        ts = datetime.datetime(2026, 6, 25, 10, 0, tzinfo=timezone.utc)
        assert _is_desk_stale(ts, now=now) is True


class TestBuildDesk:

    def _fake_conn(self, rows):
        cursor = MagicMock()
        cursor.fetchall.return_value = rows
        conn = MagicMock()
        conn.cursor.return_value = cursor
        return conn

    def _fresh(self, row):
        """Stamp a row with today's date so staleness never depends on when
        the test suite happens to run."""
        row = dict(row)
        row["ts"] = datetime.datetime.now(timezone.utc).date()
        return row

    def _stale(self, row):
        row = dict(row)
        row["ts"] = datetime.datetime.now(timezone.utc).date() - datetime.timedelta(days=5)
        return row

    def test_returns_full_desk_unlocked(self):
        btc = self._fresh(_btc_row())
        btc["asset"] = "BTC"
        eth = self._fresh(_eth_row())
        eth["asset"] = "ETH"
        conn = self._fake_conn([btc, eth])
        result = build_desk(unlocked=True, conn=conn)
        assert result["unlocked"] is True
        assert result["cta_href"] == ""
        assert [a["ticker"] for a in result["assets"]] == ["BTC", "ETH"]
        btc_values = {r["label"]: r["value"] for r in result["assets"][0]["rows"]}
        assert btc_values == _BTC_EXPECTED_UNLOCKED

    def test_as_of_uses_latest_row_date(self):
        btc = self._fresh(_btc_row())
        btc["asset"] = "BTC"
        conn = self._fake_conn([btc])
        result = build_desk(unlocked=True, conn=conn)
        assert result["as_of"] == btc["ts"].isoformat()

    def test_redaction_locked_leaves_value_only_on_free_rows(self):
        btc = self._fresh(_btc_row())
        btc["asset"] = "BTC"
        eth = self._fresh(_eth_row())
        eth["asset"] = "ETH"
        conn = self._fake_conn([btc, eth])
        result = build_desk(unlocked=False, conn=conn)
        assert result["unlocked"] is False
        for asset in result["assets"]:
            for row in asset["rows"]:
                if row["free"]:
                    assert row["value"] is not None
                else:
                    assert row["value"] is None

    def test_null_column_skipped_per_asset(self):
        btc = self._fresh(_btc_row(oi_usd=None))
        btc["asset"] = "BTC"
        conn = self._fake_conn([btc])
        result = build_desk(unlocked=True, conn=conn)
        labels = [r["label"] for r in result["assets"][0]["rows"]]
        assert "Futures OI" not in labels

    def test_stale_asset_dropped_fresh_asset_kept(self):
        btc = self._stale(_btc_row())
        btc["asset"] = "BTC"
        eth = self._fresh(_eth_row())
        eth["asset"] = "ETH"
        conn = self._fake_conn([btc, eth])
        result = build_desk(unlocked=True, conn=conn)
        assert [a["ticker"] for a in result["assets"]] == ["ETH"]

    def test_both_assets_stale_returns_none(self):
        btc = self._stale(_btc_row())
        btc["asset"] = "BTC"
        eth = self._stale(_eth_row())
        eth["asset"] = "ETH"
        conn = self._fake_conn([btc, eth])
        assert build_desk(unlocked=True, conn=conn) is None

    def test_no_rows_returns_none(self):
        conn = self._fake_conn([])
        assert build_desk(unlocked=True, conn=conn) is None

    def test_non_fatal_on_db_error_with_injected_conn(self):
        conn = MagicMock()
        conn.cursor.side_effect = RuntimeError("DB offline")
        assert build_desk(unlocked=True, conn=conn) is None

    def test_non_fatal_when_reader_raises(self):
        with patch("carrac.facts.bands.get_quant_conn", side_effect=Exception("quant DB down")):
            assert build_desk(unlocked=True) is None

    def test_query_selects_both_tickers_via_any(self):
        conn = self._fake_conn([])
        build_desk(unlocked=True, conn=conn)
        args, kwargs = conn.cursor.return_value.execute.call_args
        sql, params = args[0], args[1]
        assert "DISTINCT ON (asset)" in sql
        assert "quant.derivatives" in sql
        assert params[0] == ["BTC", "ETH"]
