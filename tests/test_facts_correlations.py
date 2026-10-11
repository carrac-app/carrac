"""Tests for carrac.facts.correlations 

_compute_shifts (PURE, no I/O)
get_correlation_shifts (DB reader — cursor mocked, no real DB)

All tests are deterministic and facts-only.
"""
from __future__ import annotations

import datetime
from unittest.mock import MagicMock, patch


from carrac.facts.correlations import _compute_shifts, get_correlation_shifts


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _row(metric_a, metric_b, correlation, q_value=None, significant=None):
    return {
        "metric_a": metric_a,
        "metric_b": metric_b,
        "correlation": correlation,
        "q_value": q_value,
        "significant": significant,
    }


# ──────────────────────────────────────────────────────────────────────────────
# _compute_shifts — PURE unit tests
# ──────────────────────────────────────────────────────────────────────────────

class TestComputeShifts:

    def test_significant_pair_large_shift_included(self):
        """A pair 0.61→0.08 (shift -0.53) that is significant today is included."""
        today = [_row("BTC", "SPX", 0.08, q_value=0.01, significant=True)]
        # prior.q_value=0.20 (>= 0.05) AND significant=False → _is_significant(prior)=False
        # so became_significant = True AND NOT False = True
        prior = [_row("BTC", "SPX", 0.61, q_value=0.20, significant=False)]
        result = _compute_shifts(today, prior, min_delta=0.25, top=6)
        assert len(result) == 1
        r = result[0]
        assert r["a"] == "BTC"
        assert r["b"] == "SPX"
        assert abs(r["prior"] - 0.61) < 0.001
        assert abs(r["today"] - 0.08) < 0.001
        assert abs(r["shift"] - (-0.53)) < 0.001
        assert r["became_significant"] is True  # today: sig flag + q=0.01; prior: not sig

    def test_small_shift_excluded(self):
        """A pair with |shift| < min_delta is excluded."""
        today = [_row("BTC", "GOLD", 0.40, q_value=0.01, significant=True)]
        prior = [_row("BTC", "GOLD", 0.50, q_value=0.01, significant=True)]
        # shift = -0.10, min_delta=0.25 → excluded
        result = _compute_shifts(today, prior, min_delta=0.25, top=6)
        assert result == []

    def test_non_significant_today_excluded(self):
        """A row that is NOT significant today is excluded even if shift is large."""
        today = [_row("ETH", "DXY", 0.08, q_value=0.20, significant=False)]
        prior = [_row("ETH", "DXY", 0.70, q_value=0.01, significant=True)]
        result = _compute_shifts(today, prior, min_delta=0.25, top=6)
        assert result == []

    def test_today_row_with_no_prior_excluded(self):
        """A today row with no matching prior pair is excluded."""
        today = [_row("SOL", "TLT", 0.50, q_value=0.01, significant=True)]
        prior = []  # no prior for this pair
        result = _compute_shifts(today, prior, min_delta=0.25, top=6)
        assert result == []

    def test_became_significant_true_when_prior_not_significant(self):
        """became_significant=True when today.significant and not prior.significant."""
        today = [_row("GOLD", "FEDFUNDS", 0.39, q_value=0.02, significant=True)]
        prior = [_row("GOLD", "FEDFUNDS", -0.05, q_value=0.40, significant=False)]
        result = _compute_shifts(today, prior, min_delta=0.25, top=6)
        assert len(result) == 1
        assert result[0]["became_significant"] is True

    def test_became_significant_false_when_both_significant(self):
        """became_significant=False when both today and prior are significant."""
        today = [_row("BTC", "TLT", -0.60, q_value=0.01, significant=True)]
        prior = [_row("BTC", "TLT", -0.20, q_value=0.01, significant=True)]
        result = _compute_shifts(today, prior, min_delta=0.25, top=6)
        assert len(result) == 1
        assert result[0]["became_significant"] is False

    def test_top_n_cap_respected(self):
        """8 candidates → only top 6 by |shift| returned."""
        today_rows = []
        prior_rows = []
        # Create 8 pairs with varying shifts (all >= 0.25)
        for i in range(8):
            shift_size = 0.30 + i * 0.05  # 0.30, 0.35, ..., 0.65
            metric_b = f"METRIC_{i}"
            today_rows.append(_row("BTC", metric_b, 0.0, q_value=0.01, significant=True))
            prior_rows.append(_row("BTC", metric_b, shift_size, q_value=0.01, significant=True))
        result = _compute_shifts(today_rows, prior_rows, min_delta=0.25, top=6)
        assert len(result) == 6
        # Largest shifts are returned first
        shifts = [abs(r["shift"]) for r in result]
        assert shifts == sorted(shifts, reverse=True)

    def test_empty_inputs_returns_empty_list(self):
        """Empty today or prior → []."""
        assert _compute_shifts([], [], min_delta=0.25, top=6) == []
        assert _compute_shifts([], [_row("BTC", "SPX", 0.5)], min_delta=0.25, top=6) == []
        assert _compute_shifts([_row("BTC", "SPX", 0.5)], [], min_delta=0.25, top=6) == []

    def test_shift_rounded_to_2dp(self):
        """shift is rounded to 2 decimal places."""
        today = [_row("BTC", "SPX", 0.081, q_value=0.01, significant=True)]
        prior = [_row("BTC", "SPX", 0.614, q_value=0.01, significant=False)]
        result = _compute_shifts(today, prior, min_delta=0.25, top=6)
        # shift = 0.081 - 0.614 = -0.533 → rounds to -0.53
        assert result[0]["shift"] == round(-0.533, 2)

    def test_significant_via_q_value_only(self):
        """A row with significant=None but q_value<0.05 is treated as significant."""
        today = [_row("BTC", "GOLD", 0.08, q_value=0.04, significant=None)]
        prior = [_row("BTC", "GOLD", 0.61, q_value=0.01, significant=None)]
        result = _compute_shifts(today, prior, min_delta=0.25, top=6)
        assert len(result) == 1

    def test_became_significant_true_via_q_value_only(self):
        """became_significant=True when today is significant via q_value<0.05 only
        (significant=None) and prior is NOT significant (q_value>=0.05, significant=None).

        A bare 'bool(row.get("significant"))' check would miss this case.
        """
        today = [_row("BTC", "GOLD", 0.08, q_value=0.03, significant=None)]
        prior = [_row("BTC", "GOLD", 0.61, q_value=0.10, significant=None)]
        result = _compute_shifts(today, prior, min_delta=0.25, top=6)
        assert len(result) == 1
        # today: q=0.03 < 0.05 → significant via _is_significant
        # prior: q=0.10 >= 0.05, significant=None → NOT significant via _is_significant
        assert result[0]["became_significant"] is True

    def test_sorted_by_abs_shift_desc(self):
        """Results are sorted largest |shift| first."""
        pairs = [
            ("BTC", "GOLD", 0.0, 0.5),    # shift -0.50
            ("BTC", "SPX",  0.0, 0.70),   # shift -0.70 (largest)
            ("BTC", "DXY",  0.0, 0.30),   # shift -0.30
        ]
        today_rows = [_row(a, b, today, q_value=0.01, significant=True) for a, b, today, _ in pairs]
        prior_rows = [_row(a, b, prior, q_value=0.01, significant=True) for a, b, _, prior in pairs]
        result = _compute_shifts(today_rows, prior_rows, min_delta=0.25, top=6)
        assert len(result) == 3
        assert result[0]["b"] == "SPX"   # largest shift -0.70
        assert result[1]["b"] == "GOLD"  # shift -0.50
        assert result[2]["b"] == "DXY"   # shift -0.30


# ──────────────────────────────────────────────────────────────────────────────
# get_correlation_shifts — DB reader (mocked cursor)
# ──────────────────────────────────────────────────────────────────────────────

class TestGetCorrelationShifts:
    """Mock the DB cursor — do NOT hit a real database."""

    def _make_row(self, metric_a, metric_b, correlation, q_value, significant, ts):
        """Return a dict-like row as the real cursor.fetchall() would return."""
        return {
            "metric_a": metric_a,
            "metric_b": metric_b,
            "correlation": correlation,
            "q_value": q_value,
            "significant": significant,
            "ts": ts,
        }

    def test_splits_and_delegates(self):
        """Verifies max_ts/prior_ts queries fire, rows split correctly, and
        _compute_shifts is called with the right today/prior lists."""
        max_ts = datetime.datetime(2026, 6, 27, 6, 0, 0)
        prior_ts = datetime.datetime(2026, 6, 20, 6, 0, 0)

        today_row = self._make_row("BTC", "SPX", 0.08, 0.01, True, max_ts)
        # prior q_value=0.20 (>= 0.05) → _is_significant(prior)=False → became_significant=True
        prior_row = self._make_row("BTC", "SPX", 0.61, 0.20, False, prior_ts)

        mock_cur = MagicMock()
        # First fetchone → max_ts
        # Second fetchone → prior_ts
        # Third fetchall → [today_row, prior_row]
        mock_cur.fetchone.side_effect = [
            {"ts": max_ts},
            {"ts": prior_ts},
        ]
        mock_cur.fetchall.return_value = [today_row, prior_row]

        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cur
        mock_conn.__enter__ = lambda s: s
        mock_conn.__exit__ = MagicMock(return_value=False)

        with patch("carrac.facts.correlations.get_conn", return_value=mock_conn):
            result = get_correlation_shifts(window=60, top=6, min_delta=0.25)

        assert len(result) == 1
        assert result[0]["a"] == "BTC"
        assert result[0]["b"] == "SPX"
        assert result[0]["became_significant"] is True

    def test_no_prior_ts_returns_empty(self):
        """When prior_ts is NULL (no rows 7+ days old), return []."""
        max_ts = datetime.datetime(2026, 6, 27, 6, 0, 0)

        mock_cur = MagicMock()
        mock_cur.fetchone.side_effect = [
            {"ts": max_ts},
            {"ts": None},  # no prior snapshot
        ]

        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cur
        mock_conn.__enter__ = lambda s: s
        mock_conn.__exit__ = MagicMock(return_value=False)

        with patch("carrac.facts.correlations.get_conn", return_value=mock_conn):
            result = get_correlation_shifts()

        assert result == []

    def test_db_exception_returns_empty(self):
        """When the DB raises, return [] (non-fatal)."""
        with patch("carrac.facts.correlations.get_conn", side_effect=Exception("DB down")):
            result = get_correlation_shifts()
        assert result == []
