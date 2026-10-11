"""Tests for dark-inventory surfacing: COT endpoint + MOVE bond_vol snapshot block.

Covers:
- _cot_stretched flag logic (pure function)
- build_cot() with synthetic DB rows: z/percentile/wow_change, sort order,
  stretched_count, market label, null-safe (no z-score row)
- build_snapshot() bond_vol block: presence, label thresholds, 5d change,
  null-safe fallback when price_vol zscores missing
- GET /api/quant/cot HTTP route (Flask test client)
- MCP quant_get_cot tool: registration, premium gate, market filter, ToolError on DB fail
- normalize.py price_vol domain query is defined and covers MOVE
"""

from datetime import date
from unittest.mock import MagicMock, patch

import pytest


# ─── Pure-function tests (no DB) ──────────────────────────────────────────────

class TestCotStretchedFlag:
    """_cot_stretched is a pure function — test all threshold combinations."""

    @pytest.fixture(autouse=True)
    def _import(self):
        from quant.api.routes import _cot_stretched
        self.f = _cot_stretched

    def test_high_abs_zscore_stretches(self):
        assert self.f(2.0, 50.0) is True
        assert self.f(-2.0, 50.0) is True
        assert self.f(3.5, 50.0) is True

    def test_zscore_below_threshold_not_stretched(self):
        assert self.f(1.9, 50.0) is False

    def test_high_percentile_stretches(self):
        assert self.f(1.5, 90.0) is True
        assert self.f(1.5, 95.0) is True
        assert self.f(0.1, 100.0) is True

    def test_low_percentile_stretches(self):
        assert self.f(0.5, 10.0) is True
        assert self.f(0.5, 9.0) is True

    def test_middle_percentile_not_stretched(self):
        assert self.f(1.5, 50.0) is False
        assert self.f(1.5, 89.9) is False

    def test_none_percentile_uses_zscore_only(self):
        assert self.f(2.1, None) is True
        assert self.f(1.9, None) is False


# ─── build_cot() helper ────────────────────────────────────────────────────────

def _make_cot_cursor(zscore_rows=None, sentiment_rows=None):
    """Build a minimal mock cursor that routes by SQL keywords."""

    class _Cursor:
        def __init__(self):
            self._sql = ''
            self._zscores = zscore_rows or []
            self._sentiment = sentiment_rows or []

        def execute(self, sql, params=None):
            self._sql = sql or ''

        def fetchall(self):
            if 'quant.zscores' in self._sql and 'sentiment' in self._sql:
                return self._zscores
            if 'quant.sentiment' in self._sql:
                return self._sentiment
            return []

        def fetchone(self):
            return None

    return _Cursor()


def _ctx_factory(cursor):
    from contextlib import contextmanager

    @contextmanager
    def _ctx():
        conn = MagicMock()
        conn.cursor.return_value = cursor
        yield conn

    return _ctx


class TestBuildCot:
    def _run(self, zscore_rows, sentiment_rows):
        """Run build_cot() with synthetic data, bypassing cache."""
        from quant.api import routes as r
        cursor = _make_cot_cursor(zscore_rows, sentiment_rows)
        # Bypass both the local dict and Redis by patching cache.get to miss
        with patch.object(r._cot_cache, 'get', return_value=None), \
             patch.object(r._cot_cache, 'set'), \
             patch('quant.api.routes.get_conn', _ctx_factory(cursor)):
            return r.build_cot()

    def _make_zscore_row(self, metric, zscore, raw_value, percentile, ts='2026-06-02'):
        """Build a dict mimicking a psycopg2 DictRow for quant.zscores."""
        d = MagicMock()
        d.__getitem__ = lambda self_, k: {
            'metric': metric, 'ts': date.fromisoformat(ts),
            'zscore': zscore, 'raw_value': raw_value, 'percentile': percentile,
        }[k]
        d.get = lambda k, default=None: {
            'metric': metric, 'ts': date.fromisoformat(ts),
            'zscore': zscore, 'raw_value': raw_value, 'percentile': percentile,
        }.get(k, default)
        # Make hasattr(d['ts'], 'isoformat') work
        d.__class__ = dict
        return {
            'metric': metric, 'ts': date.fromisoformat(ts),
            'zscore': zscore, 'raw_value': raw_value, 'percentile': percentile,
        }

    def _make_sentiment_row(self, metric, value, ts, rn):
        return {'metric': metric, 'ts': date.fromisoformat(ts),
                'value': value, 'rn': rn}

    def test_returns_dict_with_required_keys(self):
        result = self._run([], [])
        assert result is not None
        assert 'series' in result
        assert 'stretched_count' in result
        assert 'data_date' in result

    def test_all_18_metrics_present_even_without_zscores(self):
        """If there are no z-score rows, all 18 series should still appear with nulls."""
        result = self._run([], [])
        assert len(result['series']) == 18

    def test_series_with_zscore_populates_fields(self):
        zrow = self._make_zscore_row('COT_BTC_SPEC_NET', 2.5, 2467.0, 98.9)
        srow_latest = self._make_sentiment_row('COT_BTC_SPEC_NET', 2467.0, '2026-06-02', 1)
        srow_prev = self._make_sentiment_row('COT_BTC_SPEC_NET', 2280.0, '2026-05-26', 2)
        result = self._run([zrow], [srow_latest, srow_prev])
        # Find the COT_BTC_SPEC_NET entry
        s = next(x for x in result['series'] if x['metric'] == 'COT_BTC_SPEC_NET')
        assert s['zscore'] == 2.5
        assert s['value'] == 2467.0
        assert s['percentile'] == 98.9
        assert s['wow_change'] == pytest.approx(187.0)
        assert s['market'] == 'BTC'
        assert s['market_label'] == 'Bitcoin (CME)'
        assert s['type'] == 'SPEC_NET'
        assert s['date'] == '2026-06-02'

    def test_stretched_flag_set_for_high_zscore(self):
        zrow = self._make_zscore_row('COT_BTC_SPEC_NET', 2.5, 2467.0, 98.9)
        result = self._run([zrow], [])
        s = next(x for x in result['series'] if x['metric'] == 'COT_BTC_SPEC_NET')
        assert s['stretched'] is True

    def test_stretched_flag_false_for_normal(self):
        zrow = self._make_zscore_row('COT_GOLD_SPEC_NET', 0.5, 171417.0, 55.0)
        result = self._run([zrow], [])
        s = next(x for x in result['series'] if x['metric'] == 'COT_GOLD_SPEC_NET')
        assert s['stretched'] is False

    def test_sorted_by_abs_zscore_desc(self):
        rows = [
            self._make_zscore_row('COT_BTC_SPEC_NET', 1.5, 100.0, 70.0),
            self._make_zscore_row('COT_GOLD_SPEC_NET', -2.3, 200.0, 5.0),
            self._make_zscore_row('COT_OIL_SPEC_NET', 0.3, 300.0, 60.0),
        ]
        result = self._run(rows, [])
        zscores = [
            s['zscore'] for s in result['series']
            if s['zscore'] is not None
        ]
        # Should be sorted by abs(z) descending
        assert abs(zscores[0]) >= abs(zscores[1])

    def test_null_series_sorted_last(self):
        zrow = self._make_zscore_row('COT_BTC_SPEC_NET', 2.0, 100.0, 95.0)
        result = self._run([zrow], [])
        # Items with None zscore should appear last
        null_positions = [i for i, s in enumerate(result['series'])
                          if s['zscore'] is None]
        non_null_positions = [i for i, s in enumerate(result['series'])
                               if s['zscore'] is not None]
        if null_positions and non_null_positions:
            assert max(non_null_positions) < min(null_positions)

    def test_stretched_count_reflects_actual_stretched(self):
        rows = [
            self._make_zscore_row('COT_BTC_SPEC_NET', 2.5, 100.0, 99.0),   # stretched
            self._make_zscore_row('COT_ETH_SPEC_NET', -2.1, 200.0, 3.0),    # stretched
            self._make_zscore_row('COT_GOLD_SPEC_NET', 0.5, 300.0, 55.0),   # not
        ]
        result = self._run(rows, [])
        assert result['stretched_count'] == 2

    def test_wow_change_none_when_only_one_observation(self):
        srow = self._make_sentiment_row('COT_DXY_SPEC_NET', 3626.0, '2026-06-02', 1)
        result = self._run([], [srow])
        s = next(x for x in result['series'] if x['metric'] == 'COT_DXY_SPEC_NET')
        assert s['wow_change'] is None

    def test_market_labels_present_for_all_markets(self):
        result = self._run([], [])
        markets_seen = {s['market'] for s in result['series']}
        assert markets_seen == {'BTC', 'ETH', 'GOLD', 'OIL', 'SP500', 'DXY'}
        labels_seen = {s['market_label'] for s in result['series']}
        assert 'Bitcoin (CME)' in labels_seen
        assert 'Gold (COMEX)' in labels_seen


# ─── build_snapshot bond_vol block ────────────────────────────────────────────

class _SnapshotCursor:
    """Routes snapshot queries to preset return values."""

    def __init__(self, move_rows=None, move_zscore_row=None):
        self._sql = ''
        self._move_rows = move_rows or []
        self._move_zscore_row = move_zscore_row

    def execute(self, sql, params=None):
        self._sql = sql or ''

    def fetchall(self):
        sql = self._sql
        if "asset = 'MOVE'" in sql and 'price_daily' in sql:
            return self._move_rows
        if "FROM quant.macro" in sql and "ORDER BY metric" in sql:
            return []
        if "FROM quant.sentiment" in sql and "ORDER BY metric" in sql:
            return []
        if 'FROM quant.zscores z' in sql and 'ABS(z.zscore) >= 2.0' in sql:
            return []
        if "method = 'spearman'" in sql:
            return []
        if "method = 'leadlag'" in sql:
            return []
        if "source = 'chain_strength'" in sql:
            return []
        if "source = 'leadlag_summary'" in sql:
            return []
        if "method = 'leadlag_weekly'" in sql:
            return []
        return []

    def fetchone(self):
        sql = self._sql
        if "source = 'regime_hmm'" in sql:
            return None
        if "domain = 'cluster'" in sql:
            return None
        if "domain = 'price_vol'" in sql and "metric = 'MOVE'" in sql:
            return self._move_zscore_row
        return None


def _snapshot_ctx(cursor):
    from contextlib import contextmanager

    @contextmanager
    def _ctx():
        conn = MagicMock()
        conn.cursor.return_value = cursor
        yield conn

    return _ctx


def _make_price_row(ts_str, close):
    return {'ts': date.fromisoformat(ts_str), 'close': close}


class TestSnapshotBondVol:
    def _run_snapshot(self, move_rows=None, move_zscore_row=None):
        from quant.api import routes as r
        cursor = _SnapshotCursor(move_rows=move_rows,
                                 move_zscore_row=move_zscore_row)
        # Bypass both local dict and Redis by patching cache.get to miss
        with patch.object(r._qe_cache, 'get', return_value=None), \
             patch.object(r._qe_cache, 'set'), \
             patch('quant.api.routes.get_conn', _snapshot_ctx(cursor)):
            return r.build_snapshot('BTC')

    def test_bond_vol_key_present_in_snapshot(self):
        result = self._run_snapshot()
        assert result is not None
        assert 'bond_vol' in result

    def test_bond_vol_none_when_no_move_data(self):
        result = self._run_snapshot(move_rows=[], move_zscore_row=None)
        assert result['bond_vol'] is None

    def test_bond_vol_populated_with_zscore(self):
        rows = [_make_price_row('2026-06-10', 73.95)] + [
            _make_price_row(f'2026-06-0{i}', 70.0 + i) for i in range(1, 6)
        ]
        zrow = {'zscore': 0.8, 'percentile': 55.0}
        result = self._run_snapshot(move_rows=rows, move_zscore_row=zrow)
        bv = result['bond_vol']
        assert bv is not None
        assert bv['level'] == pytest.approx(73.95, rel=1e-3)
        assert bv['zscore'] == pytest.approx(0.8, rel=1e-3)
        assert bv['percentile'] == pytest.approx(55.0, rel=1e-3)

    def test_bond_vol_label_calm_below_p70(self):
        """percentile < 70 → calm."""
        rows = [_make_price_row('2026-06-10', 80.0)]
        zrow = {'zscore': 0.1, 'percentile': 55.0}
        result = self._run_snapshot(move_rows=rows, move_zscore_row=zrow)
        assert result['bond_vol']['label'] == 'calm'

    def test_bond_vol_label_elevated_between_70_and_90(self):
        """70 <= percentile < 90 → elevated."""
        rows = [_make_price_row('2026-06-10', 110.0)]
        zrow = {'zscore': 0.8, 'percentile': 80.0}
        result = self._run_snapshot(move_rows=rows, move_zscore_row=zrow)
        assert result['bond_vol']['label'] == 'elevated'

    def test_bond_vol_label_stress_at_or_above_p90(self):
        """percentile >= 90 → stress."""
        rows = [_make_price_row('2026-06-10', 135.0)]
        zrow = {'zscore': 2.1, 'percentile': 93.0}
        result = self._run_snapshot(move_rows=rows, move_zscore_row=zrow)
        assert result['bond_vol']['label'] == 'stress'

    def test_bond_vol_label_unknown_when_no_zscore(self):
        """No z-score row → label = 'unknown'."""
        rows = [_make_price_row('2026-06-10', 80.0)]
        result = self._run_snapshot(move_rows=rows, move_zscore_row=None)
        assert result['bond_vol']['label'] == 'unknown'

    def test_bond_vol_5d_change_computed(self):
        """5d_change = latest close - close 5 rows ago."""
        rows = [
            _make_price_row('2026-06-10', 73.95),
            _make_price_row('2026-06-09', 77.0),
            _make_price_row('2026-06-08', 76.0),
            _make_price_row('2026-06-07', 75.0),
            _make_price_row('2026-06-06', 74.0),
            _make_price_row('2026-06-05', 70.0),  # 5d ago
        ]
        zrow = {'zscore': 0.5, 'percentile': 50.0}
        result = self._run_snapshot(move_rows=rows, move_zscore_row=zrow)
        bv = result['bond_vol']
        assert bv['5d_change'] == pytest.approx(73.95 - 70.0, rel=1e-3)

    def test_bond_vol_5d_change_none_when_insufficient_rows(self):
        """Fewer than 6 rows → 5d_change is None."""
        rows = [_make_price_row('2026-06-10', 73.95)]
        zrow = {'zscore': 0.5, 'percentile': 50.0}
        result = self._run_snapshot(move_rows=rows, move_zscore_row=zrow)
        assert result['bond_vol']['5d_change'] is None

    def test_existing_snapshot_fields_unchanged(self):
        """bond_vol is additive — other fields must still be present."""
        result = self._run_snapshot()
        for key in ('macro', 'sentiment', 'anomalies', 'regime',
                    'top_correlations', 'leadlag_signals', 'chains'):
            assert key in result, f"snapshot key {key!r} missing"


# ─── GET /api/quant/cot HTTP endpoint ─────────────────────────────────────────

class TestCotHttpEndpoint:
    """Use a premium session — premium has quant_engine=True (member does too;
    see TestQuantEngineFeatureGate below for the member-tier test)."""

    @pytest.fixture
    def premium_client(self, client):
        with client.session_transaction() as sess:
            sess['user_id'] = 'test-user'
            sess['email'] = 'test@example.com'
            sess['tier'] = 'premium'
        return client

    def test_cot_route_exists_and_returns_json(self, premium_client):
        """Route must exist and return 200 with mocked build_cot."""
        with patch('quant.api.routes.build_cot',
                   return_value={'data_date': '2026-06-02',
                                 'stretched_count': 2,
                                 'series': []}):
            resp = premium_client.get('/api/quant/cot')
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'series' in data
        assert data['stretched_count'] == 2

    def test_cot_route_returns_500_on_db_error(self, premium_client):
        """When build_cot returns None the endpoint should return 500."""
        with patch('quant.api.routes.build_cot', return_value=None):
            resp = premium_client.get('/api/quant/cot')
        assert resp.status_code == 500


# ─── MCP quant_get_cot tool ─────────────────────────────────────────────────────────

class TestMcpGetCot:
    @pytest.fixture(scope='class', autouse=True)
    @classmethod
    def _load_registry(cls):
        pass  # triggers @mcp_tool registration

    @pytest.fixture(scope='class')
    @classmethod
    def registry(cls):
        from carrac.mcp.registry import TOOL_REGISTRY
        return TOOL_REGISTRY

    def test_get_cot_registered(self, registry):
        assert 'quant_get_cot' in registry, 'quant_get_cot MCP tool not registered'

    def test_get_cot_is_member_gated_on_quant_engine(self, registry):
        """Quant data tools follow the quant_engine knob (True for
        member/tester/premium/admin), not a hardcoded premium min_tier."""
        assert registry['quant_get_cot']['min_tier'] == 'member'
        assert registry['quant_get_cot']['feature'] == 'quant_engine'

    def test_get_cot_schema_has_market_enum(self, registry):
        schema = registry['quant_get_cot']['inputSchema']
        market_enum = set(schema['properties']['market']['enum'])
        assert {'BTC', 'ETH', 'GOLD', 'OIL', 'SP500', 'DXY', 'all'}.issubset(market_enum)

    def test_get_cot_returns_full_result_for_all(self):
        payload = {
            'data_date': '2026-06-02',
            'stretched_count': 3,
            'series': [
                {'metric': 'COT_BTC_SPEC_NET', 'market': 'BTC',
                 'market_label': 'Bitcoin (CME)', 'type': 'SPEC_NET',
                 'type_label': 'Large spec net (contracts)',
                 'value': 2467.0, 'zscore': 2.5, 'percentile': 98.9,
                 'wow_change': 187.0, 'stretched': True, 'date': '2026-06-02'},
            ],
        }
        with patch('quant.api.routes.build_cot', return_value=payload):
            from carrac.mcp.tools.quant import quant_get_cot
            result = quant_get_cot({'market': 'all'})
        assert result['stretched_count'] == 3
        assert len(result['series']) == 1

    def test_get_cot_filters_by_market(self):
        series = [
            {'metric': 'COT_BTC_SPEC_NET', 'market': 'BTC', 'stretched': True,
             'zscore': 2.5},
            {'metric': 'COT_GOLD_SPEC_NET', 'market': 'GOLD', 'stretched': False,
             'zscore': 0.3},
        ]
        payload = {'data_date': '2026-06-02', 'stretched_count': 1,
                   'series': series}
        with patch('quant.api.routes.build_cot', return_value=payload):
            from carrac.mcp.tools.quant import quant_get_cot
            result = quant_get_cot({'market': 'BTC'})
        assert all(s['market'] == 'BTC' for s in result['series'])
        assert len(result['series']) == 1
        assert result['stretched_count'] == 1

    def test_get_cot_raises_tool_error_on_invalid_market(self):
        series = [{'metric': 'COT_BTC_SPEC_NET', 'market': 'BTC',
                   'stretched': False, 'zscore': 0.1}]
        payload = {'data_date': '2026-06-02', 'stretched_count': 0,
                   'series': series}
        with patch('quant.api.routes.build_cot', return_value=payload):
            from carrac.mcp.tools.quant import quant_get_cot
            from carrac.mcp.protocol import ToolError
            with pytest.raises(ToolError, match='No COT data'):
                quant_get_cot({'market': 'SILVER'})

    def test_get_cot_raises_tool_error_on_db_failure(self):
        with patch('quant.api.routes.build_cot', return_value=None):
            from carrac.mcp.tools.quant import quant_get_cot
            from carrac.mcp.protocol import ToolError
            with pytest.raises(ToolError, match='unavailable'):
                quant_get_cot({})


# ─── normalize.py price_vol domain query ──────────────────────────────────────

class TestNormalizePriceVolDomain:
    def test_price_vol_domain_defined(self):
        from quant.normalize import DOMAIN_QUERIES
        assert 'price_vol' in DOMAIN_QUERIES, (
            'price_vol domain not in DOMAIN_QUERIES — MOVE cannot be z-scored'
        )

    def test_price_vol_query_covers_move(self):
        from quant.normalize import DOMAIN_QUERIES
        query = DOMAIN_QUERIES['price_vol']
        assert 'MOVE' in query, (
            "price_vol domain query must include 'MOVE' asset"
        )

    def test_price_vol_uses_close_column(self):
        from quant.normalize import DOMAIN_QUERIES
        query = DOMAIN_QUERIES['price_vol']
        # The domain query must use close as the value column
        assert 'close' in query.lower()

    def test_price_vol_uses_yahoo_source(self):
        from quant.normalize import DOMAIN_QUERIES
        query = DOMAIN_QUERIES['price_vol']
        assert 'yahoo' in query.lower()
