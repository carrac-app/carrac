"""
Tests for carrac.analysis — pure computation functions.
"""

from unittest.mock import patch, Mock

import numpy as np
import pandas as pd

from carrac.analysis import calculate_mansfield_rs, fetch_fear_greed_index


# ---------------------------------------------------------------------------
# Fear & Greed Index
# ---------------------------------------------------------------------------

class TestFearGreedIndex:
    @patch('carrac.analysis.requests.get')
    def test_successful_fetch(self, mock_get):
        mock_get.return_value = Mock(
            status_code=200,
            json=lambda: {'data': [{'value': '25', 'value_classification': 'Extreme Fear'}]}
        )
        result = fetch_fear_greed_index()
        assert result['value'] == 25
        assert result['classification'] == 'Extreme Fear'

    @patch('carrac.analysis.requests.get')
    def test_api_failure_withholds_rather_than_inventing(self, mock_get):
        """Sibling case: an unreachable index must not become 50/'Neutral'.

        A substituted 'Neutral' renders identically to a measured one in the
        sentiment bar, ticker, widget, MCP tool and morning report — and it
        defeats the `is not None` guards that signal_extractor, report_blocks
        and afternoon_delta already had.
        """
        mock_get.side_effect = Exception("Connection error")
        result = fetch_fear_greed_index()
        assert result['value'] is None
        assert result['classification'] is None
        assert result['error']

    @patch('carrac.analysis.requests.get')
    def test_bad_status_withholds_rather_than_inventing(self, mock_get):
        mock_get.return_value = Mock(status_code=500)
        result = fetch_fear_greed_index()
        assert result['value'] is None
        assert '500' in result['error']


# ---------------------------------------------------------------------------
# ATR Percentile
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Mansfield relative strength
# ---------------------------------------------------------------------------

class TestMansfieldRS:
    def test_basic_calculation(self):
        asset = pd.Series(np.linspace(100, 200, 100))
        benchmark = pd.Series(np.linspace(100, 150, 100))
        result = calculate_mansfield_rs(asset, benchmark, period=52)
        assert result is not None
        assert 'rs_normalized' in result
        assert 'interpretation' in result

    def test_outperformance(self):
        asset = pd.Series(np.linspace(100, 300, 100))      # 3x
        benchmark = pd.Series(np.linspace(100, 120, 100))   # 1.2x
        result = calculate_mansfield_rs(asset, benchmark, period=52)
        assert result['above_ma']
        assert result['pct_vs_benchmark'] > 0

    def test_insufficient_data_returns_none(self):
        asset = pd.Series([100, 101])
        benchmark = pd.Series([100, 99])
        result = calculate_mansfield_rs(asset, benchmark, period=52)
        assert result is None

