"""
Tests for the quant/MCP query layer (series queries, freshness
fields, correlation argument validation).

All tests are pure-Python (no live DB required) using mocks or config
inspection, following the same patterns as the existing test suite.
"""

import pytest
from unittest.mock import patch, MagicMock


# ─── Fix 1: ETF metric name in bridge.py ─────────────────────────────

class TestETFMetricNameBridge:
    """quant/bridge.py — ETF_FLOW series must query ETF_BTC_NET_FLOW
    (the name the collector actually writes), not ETF_NET_FLOW_BTC."""

    def test_etf_flow_catalog_uses_correct_metric_name(self):
        from quant.bridge import SERIES_CATALOG
        _, sql, _ = SERIES_CATALOG['ETF_FLOW']
        assert 'ETF_BTC_NET_FLOW' in sql, (
            "ETF_FLOW bridge query should use metric='ETF_BTC_NET_FLOW' "
            "(the name written by etf_flows.py collector)"
        )
        assert 'ETF_NET_FLOW_BTC' not in sql, (
            "Old incorrect metric name ETF_NET_FLOW_BTC must not appear in the query"
        )

    def test_collector_writes_etf_btc_net_flow(self):
        """Sanity check: confirm the collector still writes ETF_BTC_NET_FLOW."""
        import quant.collectors.etf_flows as etf_mod
        import inspect
        source = inspect.getsource(etf_mod)
        assert 'ETF_BTC_NET_FLOW' in source

    def test_bridge_and_collector_metric_names_agree(self):
        """Bridge SQL and collector metric name must match."""
        import re
        import quant.collectors.etf_flows as etf_mod
        import inspect
        from quant.bridge import SERIES_CATALOG

        source = inspect.getsource(etf_mod)
        # extract metric value from collector
        m = re.search(r"'metric':\s*'([^']+)'", source)
        assert m is not None, "Could not find metric name in etf_flows.py"
        collector_metric = m.group(1)

        _, sql, _ = SERIES_CATALOG['ETF_FLOW']
        assert f"metric = '{collector_metric}'" in sql, (
            f"Bridge SQL must filter metric = '{collector_metric}' "
            f"(what the collector writes)"
        )


# ─── Fix 2: MCP series query uses CANONICAL_METRICS ──────────────────

class TestMCPSeriesQuery:
    """carrac/mcp/tools/analysis.py — series query_type must resolve metric
    via CANONICAL_METRICS, not the non-existent quant.timeseries table."""

    def _call_series(self, arguments, mock_rows=None):
        """Call quant_get_data with query_type=series, mocking get_conn."""
        from carrac.mcp.tools.analysis import quant_get_data

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.__enter__ = lambda s: mock_conn
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value = mock_cur
        if mock_rows is not None:
            mock_cur.fetchall.return_value = mock_rows

        with patch('carrac.db_pool.get_quant_conn', return_value=mock_conn):
            return quant_get_data(arguments)

    def test_unknown_metric_raises_tool_error_with_valid_names(self):
        from carrac.mcp.protocol import ToolError
        from carrac.mcp.tools.analysis import quant_get_data
        with pytest.raises(ToolError) as exc:
            quant_get_data({'query_type': 'series', 'metric': 'NONEXISTENT_METRIC_XYZ'})
        assert 'Unknown metric' in str(exc.value)
        # Should list valid metric names in the error
        assert 'BTC' in str(exc.value) or 'VIX' in str(exc.value)

    def test_missing_metric_raises_tool_error(self):
        from carrac.mcp.protocol import ToolError
        from carrac.mcp.tools.analysis import quant_get_data
        with pytest.raises(ToolError) as exc:
            quant_get_data({'query_type': 'series'})
        assert 'metric is required' in str(exc.value)

    def test_series_does_not_query_timeseries_table(self):
        """quant.timeseries does not exist, so querying it fails.
        Verify that no SQL mentioning 'timeseries' is executed."""
        from carrac.mcp.tools.analysis import quant_get_data

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.__enter__ = lambda s: mock_conn
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value = mock_cur
        mock_cur.fetchall.return_value = []

        executed_sqls = []
        def capture_execute(sql, *args, **kwargs):
            executed_sqls.append(sql)
        mock_cur.execute.side_effect = capture_execute

        with patch('carrac.db_pool.get_quant_conn', return_value=mock_conn):
            quant_get_data({'query_type': 'series', 'metric': 'VIX'})

        for sql in executed_sqls:
            assert 'timeseries' not in sql.lower(), (
                "series query must not reference quant.timeseries (table does not exist)"
            )

    def test_price_metric_queries_price_daily(self):
        """A price-domain metric (BTC) should query quant.price_daily."""
        from carrac.mcp.tools.analysis import quant_get_data

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.__enter__ = lambda s: mock_conn
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value = mock_cur
        mock_cur.fetchall.return_value = []

        executed_sqls = []
        mock_cur.execute.side_effect = lambda sql, *a, **kw: executed_sqls.append(sql)

        with patch('carrac.db_pool.get_quant_conn', return_value=mock_conn):
            quant_get_data({'query_type': 'series', 'metric': 'BTC'})

        assert any('price_daily' in sql for sql in executed_sqls), (
            "BTC series should query quant.price_daily"
        )

    def test_macro_metric_queries_macro_table(self):
        """A macro-domain metric (M2) should query quant.macro."""
        from carrac.mcp.tools.analysis import quant_get_data

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.__enter__ = lambda s: mock_conn
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value = mock_cur
        mock_cur.fetchall.return_value = []

        executed_sqls = []
        mock_cur.execute.side_effect = lambda sql, *a, **kw: executed_sqls.append(sql)

        with patch('carrac.db_pool.get_quant_conn', return_value=mock_conn):
            quant_get_data({'query_type': 'series', 'metric': 'M2'})

        assert any('quant.macro' in sql for sql in executed_sqls), (
            "M2 series should query quant.macro"
        )

    def test_series_result_is_capped_at_365(self):
        """Result must be capped at 365 rows (no bulk-data export hole)."""
        from carrac.mcp.tools.analysis import quant_get_data

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.__enter__ = lambda s: mock_conn
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value = mock_cur
        mock_cur.fetchall.return_value = []

        executed_sqls = []
        mock_cur.execute.side_effect = lambda sql, *a, **kw: executed_sqls.append(sql)

        with patch('carrac.db_pool.get_quant_conn', return_value=mock_conn):
            result = quant_get_data({'query_type': 'series', 'metric': 'VIX'})

        # The cap must appear in the SQL
        assert any('365' in sql for sql in executed_sqls), (
            "Series query must LIMIT to 365 rows"
        )
        # The response shape must include a 'cap' field
        assert result.get('cap') == 365

    def test_series_response_shape(self):
        """series response must have type, metric, table, data, count, cap."""
        from carrac.mcp.tools.analysis import quant_get_data

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.__enter__ = lambda s: mock_conn
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value = mock_cur

        from datetime import date
        row = MagicMock()
        row.__getitem__ = lambda s, k: {
            'ts': date(2026, 1, 1), 'value': 123.4
        }[k]
        row.get = lambda k, d=None: {'ts': date(2026, 1, 1), 'value': 123.4}.get(k, d)
        mock_cur.fetchall.return_value = [row]

        with patch('carrac.db_pool.get_quant_conn', return_value=mock_conn):
            result = quant_get_data({'query_type': 'series', 'metric': 'VIX'})

        assert result['type'] == 'series'
        assert result['metric'] == 'VIX'
        assert 'table' in result
        assert 'data' in result
        assert 'count' in result
        assert result['cap'] == 365


# ─── Fix 3: zscores window param is now honoured ─────────────────────

class TestZScoresWindowParam:
    """carrac/mcp/tools/analysis.py — window must be used in the SQL
    AND must reject values not in {30, 90}."""

    def _call_zscores(self, window):
        from carrac.mcp.tools.analysis import quant_get_data
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.__enter__ = lambda s: mock_conn
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value = mock_cur
        mock_cur.fetchall.return_value = []
        executed = []
        executed_params = []
        mock_cur.execute.side_effect = lambda sql, params=None: (
            executed.append(sql), executed_params.append(params or [])
        )
        with patch('carrac.db_pool.get_quant_conn', return_value=mock_conn):
            result = quant_get_data({'query_type': 'zscores', 'window': window})
        return result, executed, executed_params

    def test_window_30_appears_in_sql(self):
        _, sqls, params = self._call_zscores(30)
        assert any('window_days' in sql for sql in sqls), (
            "window_days filter must appear in zscores SQL"
        )
        flat = [p for ps in params for p in (ps if isinstance(ps, list) else [ps])]
        assert 30 in flat, "window=30 must be passed as a SQL parameter"

    def test_window_90_appears_in_sql(self):
        _, sqls, params = self._call_zscores(90)
        assert any('window_days' in sql for sql in sqls)
        flat = [p for ps in params for p in (ps if isinstance(ps, list) else [ps])]
        assert 90 in flat

    def test_invalid_window_raises_tool_error(self):
        from carrac.mcp.protocol import ToolError
        from carrac.mcp.tools.analysis import quant_get_data
        for bad in (60, 120, 7, 0):
            with pytest.raises(ToolError) as exc:
                quant_get_data({'query_type': 'zscores', 'window': bad})
            assert '30 or 90' in str(exc.value), (
                f"window={bad} should raise ToolError mentioning '30 or 90'"
            )

    def test_default_window_is_90(self):
        """Omitting window should default to 90 (existing behaviour)."""
        from carrac.mcp.tools.analysis import quant_get_data
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.__enter__ = lambda s: mock_conn
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value = mock_cur
        mock_cur.fetchall.return_value = []
        executed_params = []
        mock_cur.execute.side_effect = lambda sql, params=None: executed_params.append(params or [])
        with patch('carrac.db_pool.get_quant_conn', return_value=mock_conn):
            result = quant_get_data({'query_type': 'zscores'})
        flat = [p for ps in executed_params for p in (ps if isinstance(ps, list) else [ps])]
        assert 90 in flat
        assert result['window'] == 90


# ─── Fix 4: quant_get_leadlag_ranking returns freshness + uncertainty fields ────────

class TestLeadLagFreshnessFields:
    """carrac/mcp/tools/quant.py — quant_get_leadlag_ranking must include data_as_of,
    ci_low, ci_high, n_obs on each signal entry."""

    def _make_mock_conn(self, corr_rows, persistence_rows=None):
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.__enter__ = lambda s: mock_conn
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value = mock_cur

        # fetchall returns corr_rows on first call, persistence_rows on second
        call_count = [0]
        def _fetchall():
            call_count[0] += 1
            if call_count[0] == 1:
                return corr_rows
            return persistence_rows or []
        mock_cur.fetchall.side_effect = _fetchall
        return mock_conn

    def _make_corr_row(self, metric_a='DXY', lag=14, corr=-0.6, p=0.001,
                       ts='2026-06-01', ci_low=-0.72, ci_high=-0.45, n_obs=120):
        r = MagicMock()
        r.__getitem__ = lambda s, k: {
            'metric_a': metric_a, 'lag': lag, 'correlation': corr,
            'p_value': p, 'ts': ts, 'ci_low': ci_low, 'ci_high': ci_high,
            'n_obs': n_obs,
        }[k]
        r.get = lambda k, d=None: {
            'metric_a': metric_a, 'lag': lag, 'correlation': corr,
            'p_value': p, 'ts': ts, 'ci_low': ci_low, 'ci_high': ci_high,
            'n_obs': n_obs,
        }.get(k, d)
        return r

    def test_leadlag_signal_has_data_as_of(self):
        from carrac.mcp.tools.quant import quant_get_leadlag_ranking
        row = self._make_corr_row(metric_a='DXY')
        mock_conn = self._make_mock_conn([row])

        with patch('carrac.db_pool.get_quant_conn', return_value=mock_conn):
            result = quant_get_leadlag_ranking({'target': 'BTC'})

        signals = result['signals']
        assert len(signals) > 0
        for sig in signals:
            assert 'data_as_of' in sig, "data_as_of missing from quant_get_leadlag_ranking signal"

    def test_leadlag_signal_has_ci_fields(self):
        from carrac.mcp.tools.quant import quant_get_leadlag_ranking
        row = self._make_corr_row(metric_a='DXY', ci_low=-0.72, ci_high=-0.45)
        mock_conn = self._make_mock_conn([row])

        with patch('carrac.db_pool.get_quant_conn', return_value=mock_conn):
            result = quant_get_leadlag_ranking({'target': 'BTC'})

        signals = result['signals']
        assert len(signals) > 0
        for sig in signals:
            assert 'ci_low' in sig, "ci_low missing from quant_get_leadlag_ranking signal"
            assert 'ci_high' in sig, "ci_high missing from quant_get_leadlag_ranking signal"

    def test_leadlag_signal_has_n_obs(self):
        from carrac.mcp.tools.quant import quant_get_leadlag_ranking
        row = self._make_corr_row(metric_a='DXY', n_obs=120)
        mock_conn = self._make_mock_conn([row])

        with patch('carrac.db_pool.get_quant_conn', return_value=mock_conn):
            result = quant_get_leadlag_ranking({'target': 'BTC'})

        signals = result['signals']
        assert len(signals) > 0
        for sig in signals:
            assert 'n_obs' in sig, "n_obs missing from quant_get_leadlag_ranking signal"

    def test_leadlag_ci_values_are_correct(self):
        from carrac.mcp.tools.quant import quant_get_leadlag_ranking
        row = self._make_corr_row(
            metric_a='DXY', ci_low=-0.72, ci_high=-0.45, n_obs=120, ts='2026-06-01'
        )
        mock_conn = self._make_mock_conn([row])

        with patch('carrac.db_pool.get_quant_conn', return_value=mock_conn):
            result = quant_get_leadlag_ranking({'target': 'BTC'})

        sig = result['signals'][0]
        assert sig['ci_low'] == pytest.approx(-0.72, abs=0.001)
        assert sig['ci_high'] == pytest.approx(-0.45, abs=0.001)
        assert sig['n_obs'] == 120
        assert sig['data_as_of'] == '2026-06-01'

    def test_leadlag_null_ci_fields_are_none(self):
        """ci_low/ci_high that are NULL in DB should come back as None."""
        from carrac.mcp.tools.quant import quant_get_leadlag_ranking
        row = self._make_corr_row(metric_a='DXY', ci_low=None, ci_high=None)
        mock_conn = self._make_mock_conn([row])

        with patch('carrac.db_pool.get_quant_conn', return_value=mock_conn):
            result = quant_get_leadlag_ranking({'target': 'BTC'})

        sig = result['signals'][0]
        assert sig['ci_low'] is None
        assert sig['ci_high'] is None

    def test_leadlag_docstring_mentions_ci_is_of_correlation(self):
        """The docstring must clarify that CI is of the correlation coefficient,
        not of any projected move."""
        # get the mcp_tool decorator description, which is in the tool registry
        from carrac.mcp.registry import TOOL_REGISTRY
        desc = TOOL_REGISTRY['quant_get_leadlag_ranking']['description']
        assert 'ci_low/ci_high are the 95% CI of the correlation coefficient' in desc


class TestTransmissionChainsDataAsOf:
    """carrac/mcp/tools/quant.py — quant_get_transmission_chains must include
    data_as_of on each chain entry."""

    def test_chain_result_has_data_as_of(self):
        from carrac.mcp.tools.quant import quant_get_transmission_chains

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.__enter__ = lambda s: mock_conn
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value = mock_cur

        # Build mock signal rows for the chain links
        def _make_row(metric, ts='2026-06-01'):
            r = MagicMock()
            r.__getitem__ = lambda s, k: {
                'ts': ts, 'metric': metric, 'value': 7,
                'metadata': {'best_r': 0.55, 'persistent': True},
            }[k]
            r.get = lambda k, d=None: {
                'ts': ts, 'metric': metric, 'value': 7,
                'metadata': {'best_r': 0.55, 'persistent': True},
            }.get(k, d)
            return r

        # monetary_policy links: FED_FUNDS__YIELD_2Y, YIELD_2Y__YIELD_10Y,
        # YIELD_10Y__HY_SPREAD, HY_SPREAD__SPX, SPX__BTC
        link_metrics = [
            'FED_FUNDS__YIELD_2Y', 'YIELD_2Y__YIELD_10Y',
            'YIELD_10Y__HY_SPREAD', 'HY_SPREAD__SPX', 'SPX__BTC',
        ]
        mock_cur.fetchall.return_value = [_make_row(m) for m in link_metrics]

        with patch('carrac.db_pool.get_quant_conn', return_value=mock_conn):
            result = quant_get_transmission_chains({'chain': 'monetary_policy'})

        for chain in result['chains']:
            assert 'data_as_of' in chain, (
                f"data_as_of missing from quant_get_transmission_chains chain {chain.get('chain')}"
            )
            assert chain['data_as_of'] == '2026-06-01'


# ─── Fix 5: sparse-series z-score latest_only mode ───────────────────

class TestSparseSeriesLatestOnly:
    """quant/normalize.py — latest_only mode must use ROW_NUMBER() per
    (metric, asset) NOT a 91-calendar-day date filter.

    These tests complement the updated TestIncrementalNormalize class
    in test_quant.py, focusing on the specific sparse-series fix."""

    def _run_process_domain(self, latest_only, query=None):
        """Run _process_domain with a mocked DB and return the executed SQL."""
        from unittest.mock import patch, MagicMock
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.__enter__ = lambda s: mock_conn
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value = mock_cur
        mock_cur.fetchall.return_value = []
        executed_sqls = []
        mock_cur.execute.side_effect = lambda sql, *a, **kw: executed_sqls.append(sql)

        base_query = query or 'SELECT ts, metric, asset, value FROM quant.macro'
        with patch('quant.normalize.get_conn', return_value=mock_conn):
            from quant.normalize import _process_domain
            _process_domain('test', base_query, dry_run=True, latest_only=latest_only)
        return executed_sqls

    def test_latest_only_does_not_use_date_interval_filter(self):
        """Fix: must NOT use INTERVAL '91 days' date filter — that broke
        monthly series (M2) by yielding only ~3 points per group."""
        sqls = self._run_process_domain(latest_only=True)
        assert len(sqls) == 1
        assert "INTERVAL '91 days'" not in sqls[0], (
            "latest_only must not use a calendar-day filter "
            "(breaks monthly series like M2)"
        )

    def test_latest_only_uses_row_number_per_group(self):
        sqls = self._run_process_domain(latest_only=True)
        sql = sqls[0]
        assert 'ROW_NUMBER()' in sql
        assert 'PARTITION BY metric, asset' in sql
        assert 'ORDER BY ts DESC' in sql

    def test_latest_only_keeps_91_observations(self):
        """Must request 91 observations (enough for 90-day window + 1)."""
        sqls = self._run_process_domain(latest_only=True)
        assert 'rn <= 91' in sqls[0]

    def test_full_mode_unchanged(self):
        """Full-mode path must not be affected by the fix."""
        original = 'SELECT ts, metric, asset, value FROM quant.macro'
        sqls = self._run_process_domain(latest_only=False, query=original)
        assert len(sqls) == 1
        assert sqls[0] == original

    def test_latest_only_wraps_different_domain_queries(self):
        """The wrapper must work for any domain query, not just macro."""
        from quant.normalize import DOMAIN_QUERIES
        for domain, query in list(DOMAIN_QUERIES.items())[:3]:
            sqls = self._run_process_domain(latest_only=True, query=query)
            assert len(sqls) == 1
            sql = sqls[0]
            assert 'ROW_NUMBER()' in sql, (
                f"Domain {domain}: ROW_NUMBER() wrapper missing in latest_only mode"
            )
            # The wrapper must embed the original query as an inner subquery
            assert '_inner' in sql or '_ranked' in sql


# ─── QA sweep fix: quant_get_correlations fails fast on unknown target ──────

class TestCorrelationsUnknownTargetFailsFast:
    """analysis.py:quant_get_correlations — an unrecognised target must not fall
    through to a DB query that hangs to the full MCP timeout instead of
    erroring cleanly (unlike quant_get_leadlag_ranking, which validates up front)."""

    def test_unknown_target_raises_without_touching_db(self):
        from carrac.mcp.protocol import ToolError
        from carrac.mcp.tools.analysis import quant_get_correlations

        with patch('carrac.db_pool.get_quant_conn') as mock_get_conn:
            with pytest.raises(ToolError) as exc:
                quant_get_correlations({'target': 'ZZZZNOTREAL'})
            mock_get_conn.assert_not_called()
        assert 'ZZZZNOTREAL' in str(exc.value)
        assert 'Valid targets' in str(exc.value)

    def test_known_target_still_queries_db(self):
        from carrac.mcp.tools.analysis import quant_get_correlations

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.__enter__ = lambda s: mock_conn
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value = mock_cur
        mock_cur.fetchall.return_value = []
        with patch('carrac.db_pool.get_quant_conn', return_value=mock_conn):
            result = quant_get_correlations({'target': 'BTC'})
        assert result['target'] == 'BTC'
        mock_cur.execute.assert_called_once()

    def test_lowercase_target_is_normalized(self):
        """target should be case-insensitive, matching CANONICAL_METRICS keys."""
        from carrac.mcp.tools.analysis import quant_get_correlations

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.__enter__ = lambda s: mock_conn
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value = mock_cur
        mock_cur.fetchall.return_value = []
        with patch('carrac.db_pool.get_quant_conn', return_value=mock_conn):
            result = quant_get_correlations({'target': 'btc'})
        assert result['target'] == 'BTC'


# ─── quant_get_correlations rejects an unsupported window ──────

class TestCorrelationsUnsupportedWindowFailsFast:
    """analysis.py:quant_get_correlations — window=90/20/0/9999 must not fall
    through to a query that legitimately finds zero rows (only 60 and 120
    are ever written by quant/engine/correlations.py: CORRELATION_WINDOWS =
    [60, 120]) and echo the caller's own number back in
    {"window": 20, "count": 0}. An LLM reads that as "no correlations on a
    20-day window" — a false statement about the market. Mirrors
    quant_get_data's window check (carrac/mcp/tools/analysis.py::quant_get_data)."""

    @pytest.mark.parametrize('bad_window', [90, 20, 0, 9999, -30])
    def test_unsupported_window_raises_without_touching_db(self, bad_window):
        from carrac.mcp.protocol import ToolError
        from carrac.mcp.tools.analysis import quant_get_correlations

        with patch('carrac.db_pool.get_quant_conn') as mock_get_conn:
            with pytest.raises(ToolError) as exc:
                quant_get_correlations({'target': 'BTC', 'window': bad_window})
            mock_get_conn.assert_not_called()
        msg = str(exc.value)
        assert str(bad_window) in msg
        assert '60' in msg and '120' in msg

    @pytest.mark.parametrize('good_window', [60, 120])
    def test_supported_window_still_queries_db(self, good_window):
        from carrac.mcp.tools.analysis import quant_get_correlations

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.__enter__ = lambda s: mock_conn
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value = mock_cur
        mock_cur.fetchall.return_value = []
        with patch('carrac.db_pool.get_quant_conn', return_value=mock_conn):
            result = quant_get_correlations({'target': 'BTC', 'window': good_window})
        assert result['window'] == good_window
        mock_cur.execute.assert_called_once()

    def test_default_window_is_supported(self):
        """window omitted entirely must still hit the DB with the documented
        default of 60, not raise."""
        from carrac.mcp.tools.analysis import quant_get_correlations

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.__enter__ = lambda s: mock_conn
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value = mock_cur
        mock_cur.fetchall.return_value = []
        with patch('carrac.db_pool.get_quant_conn', return_value=mock_conn):
            result = quant_get_correlations({'target': 'BTC'})
        assert result['window'] == 60


# ─── quant_get_correlations returns one row per metric ──────

class TestCorrelationsOneRowPerMetric:
    """analysis.py:quant_get_correlations — the query must filter on `method`;
    otherwise the same (metric_a, metric_b, window_days, ts)
    also matched the regime-split rows the engine writes under
    method='spearman_favorable'/'spearman_unfavorable'
    (quant/engine/correlations.py::compute_rolling_correlations) — same metric, same date, same
    requested window, different correlation and a smaller n_obs (e.g. 36
    instead of the full window's 60). The query filters to
    method='spearman' (the unconditional full-window correlation), matching
    the HTTP route's own canonical query (quant/api/routes.py::build_snapshot)."""

    def _run(self, rows):
        from carrac.mcp.tools.analysis import quant_get_correlations

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.__enter__ = lambda s: mock_conn
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value = mock_cur
        mock_cur.fetchall.return_value = rows
        with patch('carrac.db_pool.get_quant_conn', return_value=mock_conn):
            result = quant_get_correlations({'target': 'BTC', 'window': 60})
        return result, mock_cur

    def test_query_filters_to_the_unconditional_method(self):
        """The SQL itself must exclude the regime-split/lead-lag rows —
        dedup belongs in the WHERE clause, not a Python-side workaround,
        because method is part of the table's primary key
        (ts, metric_a, metric_b, window_days, method): fixing method to a
        single value is what makes metric_b unique per ts/window."""
        result, mock_cur = self._run(rows=[])
        sql = mock_cur.execute.call_args[0][0]
        assert "method = 'spearman'" in sql
        assert result['data'] == []

    def test_no_duplicate_metric_in_response(self):
        """Simulates what Postgres would return once the WHERE clause is
        correctly scoped to method='spearman' — a single row per metric_b,
        not a pair of n_obs=36/n_obs=60 rows for the same metric."""
        rows = [
            {'metric_a': 'BTC', 'metric_b': 'ETH', 'correlation': 0.874,
             'p_value': 0.0001, 'n_obs': 60, 'ts': '2026-08-11',
             'q_value': 0.001, 'ci_low': 0.798, 'ci_high': 0.923,
             'significant': True},
            {'metric_a': 'BTC', 'metric_b': 'SOL', 'correlation': 0.804,
             'p_value': 0.0002, 'n_obs': 60, 'ts': '2026-08-11',
             'q_value': 0.002, 'ci_low': 0.691, 'ci_high': 0.879,
             'significant': True},
        ]
        result, _ = self._run(rows)
        metrics = [entry['metric'] for entry in result['data']]
        assert metrics == ['ETH', 'SOL']
        assert len(metrics) == len(set(metrics)), (
            'each metric must appear at most once per (target, window) call'
        )


# ─── QA sweep fix: quant_get_data zscores latest-per-metric join ──────

class TestZscoresLatestPerMetricJoin:
    """analysis.py:quant_get_data(query_type='zscores') — a single GLOBAL MAX(ts) across the whole quant.zscores table, so
    any domain that isn't the most-recently-updated one (e.g. macro, which
    updates on FRED/macro release schedules, not every worker cycle) never
    shared that exact timestamp and always came back empty — indistinguishable
    from 'nothing anomalous'. The query joins the latest ts PER
    (domain, metric, asset), mirroring quant_get_anomalies."""

    def _call(self, arguments, rows=None):
        from carrac.mcp.tools.analysis import quant_get_data
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.__enter__ = lambda s: mock_conn
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value = mock_cur
        mock_cur.fetchall.return_value = rows or []
        executed = []
        mock_cur.execute.side_effect = lambda sql, params=None: executed.append((sql, params))
        with patch('carrac.db_pool.get_quant_conn', return_value=mock_conn):
            result = quant_get_data(arguments)
        return result, executed

    def test_query_groups_by_domain_metric_asset_not_global_max(self):
        result, executed = self._call({'query_type': 'zscores', 'domain': 'macro'})
        sql, params = executed[0]
        assert 'GROUP BY domain, metric, asset' in sql, (
            'zscores query must compute latest ts per (domain, metric, asset), '
            'not a single global MAX(ts)'
        )
        assert 'domain' in result

    def test_derivatives_domain_uses_like_pattern_for_subdomains(self):
        """'derivatives' is a friendly umbrella over derivatives_funding/ls/
        options/advanced — must expand to a LIKE match, not an exact-equals
        filter that would never match any literal 'derivatives' row."""
        _, executed = self._call({'query_type': 'zscores', 'domain': 'derivatives'})
        sql, params = executed[0]
        assert "derivatives%" in sql
        # 'derivatives' itself must not be passed as a bound exact-match param
        flat = [p for _, ps in executed for p in (ps or [])]
        assert 'derivatives' not in flat

    def test_macro_domain_uses_exact_match(self):
        _, executed = self._call({'query_type': 'zscores', 'domain': 'macro'})
        sql, params = executed[0]
        flat = [p for _, ps in executed for p in (ps or [])]
        assert 'macro' in flat

    def test_empty_result_for_filtered_domain_includes_explicit_message(self):
        """An empty result for a domain filter must not read like a bare
        'nothing anomalous' — it must explicitly say the domain has no data."""
        result, _ = self._call({'query_type': 'zscores', 'domain': 'price'}, rows=[])
        assert result['data'] == []
        assert 'message' in result
        assert 'price' in result['message']

    def test_no_message_when_domain_omitted(self):
        """Without a domain filter, an empty result is a normal (if
        unlikely) outcome — no message needed."""
        result, _ = self._call({'query_type': 'zscores'}, rows=[])
        assert 'message' not in result

    def test_no_message_when_data_present(self):
        rows = [{'domain': 'sentiment', 'metric': 'FEAR_GREED', 'asset': '_GLOBAL_',
                  'zscore': 1.2, 'raw_value': 50.0}]
        result, _ = self._call({'query_type': 'zscores', 'domain': 'sentiment'}, rows=rows)
        assert 'message' not in result
        assert len(result['data']) == 1
