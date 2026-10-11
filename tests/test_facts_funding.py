"""Tests for carrac/facts/funding.py — fetch_funding_history.

All tests mock the DB cursor; no real database is required.
"""
from __future__ import annotations

import contextlib
from unittest.mock import MagicMock



# ---------------------------------------------------------------------------
# Helper: build a fake conn + cursor that returns preset rows (dict-shaped)
# ---------------------------------------------------------------------------

def _fake_conn(rows: list[dict]) -> MagicMock:
    """Return a mock conn whose cursor().fetchall() returns `rows` (dict-keyed)."""
    cursor = MagicMock()
    cursor.fetchall.return_value = rows

    conn = MagicMock()
    conn.cursor.return_value = cursor
    return conn


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestFetchFundingHistory:

    def test_groups_rows_by_asset(self):
        """Rows for two assets → dict keyed by asset with values in order."""
        # Ordered by asset, ts (BTC before ETH alphabetically)
        rows = [
            {"ts": "2024-01-01", "asset": "BTC", "v": 0.01},
            {"ts": "2024-01-02", "asset": "BTC", "v": 0.02},
            {"ts": "2024-01-01", "asset": "ETH", "v": -0.01},
            {"ts": "2024-01-02", "asset": "ETH", "v": -0.02},
        ]
        conn = _fake_conn(rows)

        from carrac.facts.funding import fetch_funding_history
        result = fetch_funding_history(90, conn=conn)

        assert "BTC" in result
        assert "ETH" in result
        assert result["BTC"] == [0.01, 0.02]
        assert result["ETH"] == [-0.01, -0.02]

    def test_interleaved_rows_grouped_correctly(self):
        """Rows interleaved (not ordered by asset) are still grouped correctly."""
        rows = [
            {"ts": "2024-01-01", "asset": "BTC", "v": 0.01},
            {"ts": "2024-01-01", "asset": "ETH", "v": -0.01},
            {"ts": "2024-01-02", "asset": "BTC", "v": 0.02},
            {"ts": "2024-01-02", "asset": "ETH", "v": -0.02},
        ]
        conn = _fake_conn(rows)

        from carrac.facts.funding import fetch_funding_history
        result = fetch_funding_history(90, conn=conn)

        assert result["BTC"] == [0.01, 0.02]
        assert result["ETH"] == [-0.01, -0.02]

    def test_empty_result_returns_empty_dict(self):
        """No rows → empty dict (no assets covered)."""
        conn = _fake_conn([])

        from carrac.facts.funding import fetch_funding_history
        result = fetch_funding_history(90, conn=conn)

        assert result == {}

    def test_single_asset_single_row(self):
        rows = [{"ts": "2024-01-01", "asset": "SOLUSDT", "v": 0.05}]
        conn = _fake_conn(rows)

        from carrac.facts.funding import fetch_funding_history
        result = fetch_funding_history(90, conn=conn)

        assert result == {"SOLUSDT": [0.05]}

    def test_values_cast_to_float(self):
        """Values from the DB row are cast to float."""
        rows = [{"ts": "2024-01-01", "asset": "BTC", "v": 3}]  # int v
        conn = _fake_conn(rows)

        from carrac.facts.funding import fetch_funding_history
        result = fetch_funding_history(90, conn=conn)

        assert isinstance(result["BTC"][0], float)

    def test_non_fatal_on_db_error(self):
        """Any exception from the cursor → return {} (non-fatal)."""
        conn = MagicMock()
        conn.cursor.side_effect = RuntimeError("DB offline")

        from carrac.facts.funding import fetch_funding_history
        result = fetch_funding_history(90, conn=conn)

        assert result == {}

    def test_non_fatal_on_fetchall_error(self):
        """fetchall() raising → return {}."""
        cursor = MagicMock()
        cursor.fetchall.side_effect = Exception("query failed")
        conn = MagicMock()
        conn.cursor.return_value = cursor

        from carrac.facts.funding import fetch_funding_history
        result = fetch_funding_history(90, conn=conn)

        assert result == {}

    def test_preserves_order_within_asset(self):
        """Values within each asset are in the order they appear in the result rows."""
        rows = [
            {"ts": "2024-01-01", "asset": "BTC", "v": 0.10},
            {"ts": "2024-01-02", "asset": "BTC", "v": 0.20},
            {"ts": "2024-01-03", "asset": "BTC", "v": 0.30},
        ]
        conn = _fake_conn(rows)

        from carrac.facts.funding import fetch_funding_history
        result = fetch_funding_history(90, conn=conn)

        assert result["BTC"] == [0.10, 0.20, 0.30]


class TestFetchFundingHistoryContextManagerPath:
    """Real-path tests: conn=None forces the context-manager path via carrac.db_pool.get_quant_conn.

    get_conn() is a context manager and must be entered with 'with'; calling
    .cursor() on it directly raises AttributeError.
    """

    def test_contextmanager_path_groups_correctly(self, monkeypatch):
        """conn=None → uses 'with get_conn() as c' context manager, reads dict rows."""

        @contextlib.contextmanager
        def _fake_get_conn():
            cursor = MagicMock()
            cursor.fetchall.return_value = [
                {"ts": "2024-01-01", "asset": "BTC", "v": 0.01},
                {"ts": "2024-01-02", "asset": "BTC", "v": 0.02},
                {"ts": "2024-01-01", "asset": "ETH", "v": -0.01},
            ]
            conn = MagicMock()
            conn.cursor.return_value = cursor
            yield conn

        # Patch the quant pool accessor so the inline import inside
        # fetch_funding_history resolves to our contextmanager stub.
        monkeypatch.setattr("carrac.db_pool.get_quant_conn", _fake_get_conn)

        from carrac.facts.funding import fetch_funding_history
        result = fetch_funding_history(90)  # no conn= → triggers the contextmanager path

        assert "BTC" in result
        assert "ETH" in result
        assert result["BTC"] == [0.01, 0.02]
        assert result["ETH"] == [-0.01]

    def test_contextmanager_path_non_fatal_on_exception(self, monkeypatch):
        """conn=None path still returns {} on any exception (non-fatal contract)."""

        @contextlib.contextmanager
        def _raising_get_conn():
            raise RuntimeError("quant DB unavailable")
            yield  # unreachable, but keeps it a valid contextmanager

        monkeypatch.setattr("carrac.db_pool.get_quant_conn", _raising_get_conn)

        from carrac.facts.funding import fetch_funding_history
        result = fetch_funding_history(90)

        assert result == {}
