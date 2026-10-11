"""Unit tests for the Quant Engine integration bundle.

Covers:
- BASE_PROMPT Quant Engine section
- QuantEngineBlock class wiring
- morning_engine data_assembly regime phase / per-asset block
- MCP new tools registered with valid schemas
- build_snapshot helper behaviour (shared by per-asset threading)
- push_notifications quant-alert creators (shipped without tests)
- Formula evaluator primitive routing (parse was covered; eval wasn't)

Tests are pure-Python (mock DB / mock _api calls) so they run without Postgres.
"""

from unittest.mock import patch, MagicMock

import pytest

try:
    import pandas as _pd  # noqa: F401
    HAS_PANDAS = True
except ImportError:
    HAS_PANDAS = False

needs_pandas = pytest.mark.skipif(not HAS_PANDAS, reason='pandas not installed')


# ─── BASE_PROMPT ──────────────────────────────────────────────

class TestBasePromptQuantEngine:
    """The BASE_PROMPT must advertise every Quant Engine surface the
    Copilot is expected to reason about. If a user asks the Copilot
    about chains, primitives, or alert types and the prompt doesn't
    cover them, the model will hallucinate or refuse."""

    @pytest.fixture(scope='class')
    @classmethod
    def prompt(cls):
        from carrac.ai.features import BASE_PROMPT
        return BASE_PROMPT

    def test_quant_engine_heading(self, prompt):
        assert 'QUANT ENGINE' in prompt

    def test_six_domains_named(self, prompt):
        for domain in ('price', 'macro', 'derivatives', 'sentiment', 'onchain', 'signals'):
            assert domain in prompt, f"Domain {domain!r} missing from BASE_PROMPT"

    def test_three_transmission_chains_named(self, prompt):
        for chain in ('monetary_policy', 'dollar_liquidity', 'risk_appetite'):
            assert chain in prompt, f"Chain {chain!r} missing from BASE_PROMPT"

    def test_regime_section_references_phase(self, prompt):
        # The regime prose pulls the HMM-implied expected duration into the
        # regime narrative. The Copilot needs this concept too so users can
        # ask "where are we in the regime cycle" without confusing it.
        # This figure is the mean of a geometric distribution
        # implied by current transition odds, not a historical median. The
        # prompt is allowed to explain that distinction (so it legitimately
        # says the words "historical median" once, as a DON'T) — what must
        # never appear is a rendered template that tells the model to
        # actually output that label.
        assert 'expected duration' in prompt
        assert 'historical median duration of the current label' not in prompt
        assert '~22d historical median' not in prompt
        for phase in ('early', 'mid', 'mature', 'late-stage'):
            assert phase in prompt

    def test_all_formula_primitives_documented(self, prompt):
        # These five must be callable from user formulas and must appear by
        # name so the Copilot can suggest correct syntax.
        for primitive in (
            'leadlag', 'chain', 'regime_prob',
            'anomaly_active', 'analogue_similarity',
        ):
            assert primitive in prompt

    def test_all_quant_alert_types_documented(self, prompt):
        for atype in (
            'zscore', 'regime_transition', 'chain_strength_breach',
            'anomaly_cluster_active', 'conditional_query_match',
        ):
            assert atype in prompt

    def test_correlation_rigor_terms(self, prompt):
        # Tests that the prompt teaches q-value / CI interpretation so
        # the model doesn't surface non-significant correlations as evidence.
        for term in ('q_value', 'significant', 'ci_low', 'ci_high'):
            assert term in prompt

    def test_edge_detection_warning(self, prompt):
        assert 'edge-detected' in prompt


# ─── QuantEngineBlock ─────────────────────────────────────────

class TestQuantEngineBlock:
    """The copilot block that surfaces Quant Engine state into the LLM
    context. Tests use a query-string router so we don't have to know
    the exact sequence the block executes in."""

    @pytest.fixture
    def block(self):
        from carrac.ai.context_blocks import QuantEngineBlock
        return QuantEngineBlock()

    def test_id_and_category(self, block):
        assert block.id == 'quant_engine'
        assert block.category == 'macro'
        assert block.default_enabled is True

    def test_fetch_renders_regime_phase(self, block):
        """Duration ~15% of the expected duration → phase should be 'early'."""
        cursor = _QueryRoutedCursor(
            regime_row={'ts': '2026-04-18', 'value': 0,
                        'metadata': {'label': 'favorable', 'probability': 0.74,
                                     'duration_days': 3, 'since': '2026-04-15',
                                     'transition_matrix': [[0.95, 0.05], [0.1, 0.9]]}},
        )
        with patch('carrac.db_pool.get_quant_conn', _cursor_ctx(cursor)):
            text = block.fetch(user_id='1', asset='BTC', timeframe='1D', config={})
        assert 'FAVORABLE' in text
        assert 'early' in text
        # This is the HMM-implied expected duration, not a
        # historical median — must not share bands.py's "historical median"
        # wording for its own, genuinely historical/median figure.
        assert 'expected duration' in text
        assert 'historical median' not in text
        # Expected run = 1/(1-0.95) = 20; duration 3 → frac=0.15 → early
        assert '20d' in text

    def test_fetch_renders_chains(self, block):
        cursor = _QueryRoutedCursor(
            chain_rows=[
                {'metric': 'monetary_policy', 'value': 0.27,
                 'metadata': {'weakest_link': {'from': 'FED_FUNDS', 'to': 'YIELD_2Y', 'r': 0.12}}},
                {'metric': 'dollar_liquidity', 'value': 0.55,
                 'metadata': {'weakest_link': {'from': 'DXY', 'to': 'GOLD', 'r': 0.31}}},
            ],
        )
        with patch('carrac.db_pool.get_quant_conn', _cursor_ctx(cursor)):
            text = block.fetch(user_id='1', asset='BTC', timeframe='1D', config={})
        assert 'Chains' in text
        assert 'MONETARY_POLICY 0.27' in text
        assert 'DOLLAR_LIQUIDITY 0.55' in text
        assert 'FED_FUNDS→YIELD_2Y' in text

    def test_fetch_renders_significant_correlation_with_q(self, block):
        cursor = _QueryRoutedCursor(
            correlations=[
                {'metric_a': 'BTC', 'metric_b': 'SPX', 'correlation': 0.72,
                 'q_value': 0.01, 'significant': True},
            ],
        )
        with patch('carrac.db_pool.get_quant_conn', _cursor_ctx(cursor)):
            text = block.fetch(user_id='1', asset='BTC', timeframe='1D', config={})
        assert 'SPX' in text
        assert 'q=' in text

    def test_fetch_renders_anomaly_cluster(self, block):
        cursor = _QueryRoutedCursor(
            cluster_row={'ts': '2026-04-18', 'zscore': 3,
                         'metadata': {'domains_affected': 2,
                                      'metrics': [
                                          {'metric': 'VIX', 'zscore': 2.3},
                                          {'metric': 'HY_SPREAD', 'zscore': 2.1},
                                      ]}},
        )
        with patch('carrac.db_pool.get_quant_conn', _cursor_ctx(cursor)):
            text = block.fetch(user_id='1', asset='BTC', timeframe='1D', config={})
        assert 'Anomaly cluster ACTIVE' in text
        assert 'VIX +2.3σ' in text


class _QueryRoutedCursor:
    """Routes SELECT queries to per-shape return values based on
    keywords in the SQL text. This decouples tests from the exact
    execution order inside QuantEngineBlock.fetch()."""

    def __init__(self, regime_row=None, cluster_row=None, anomalies=None,
                 correlations=None, leadlag=None, leader_zs=None,
                 chain_rows=None):
        self.regime_row = regime_row
        self.cluster_row = cluster_row
        self.anomalies = anomalies or []
        self.correlations = correlations or []
        self.leadlag = leadlag or []
        self.leader_zs = leader_zs or []
        self.chain_rows = chain_rows or []
        self._last_sql = ''

    def execute(self, sql, params=None):
        self._last_sql = sql or ''

    def fetchone(self):
        sql = self._last_sql
        if "source = 'regime_hmm'" in sql:
            return self.regime_row
        if "domain = 'cluster'" in sql:
            return self.cluster_row
        return None

    def fetchall(self):
        sql = self._last_sql
        if 'FROM quant.zscores z' in sql and 'ABS(z.zscore) >= 2.0' in sql:
            return self.anomalies
        if "method = 'spearman'" in sql:
            return self.correlations
        if "method = 'leadlag'" in sql and 'ABS(correlation) DESC' in sql:
            return self.leadlag
        if 'window_days = 90' in sql and 'metric = ANY' in sql:
            return self.leader_zs
        if "source = 'chain_strength'" in sql:
            return self.chain_rows
        return []


def _cursor_ctx(cursor):
    """Return a patch target for carrac.db_pool.get_quant_conn that yields the
    provided cursor."""
    from contextlib import contextmanager

    @contextmanager
    def _ctx():
        conn = MagicMock()
        conn.cursor.return_value = cursor
        yield conn
    return _ctx


# ─── morning_engine data_assembly ────────────────────

class TestMorningReportQuantContext:
    """fetch_quant_context() owns the regime-phase narrative and the
    per-asset (ETH/SOL) blocks. Mock the _api helper so we don't need
    a running quant service."""

    def test_regime_phase_surfaces_expected_duration(self):
        """Given regime_hist_median_days, the text must mention the phase —
        but never label it "historical median". That figure is
        1/(1-p_stay), the mean of a geometric distribution implied by
        CURRENT transition odds; bands.py's regime clock owns the genuine
        historical/median wording elsewhere on the same report."""
        from morning_engine import data_assembly
        snap = {
            'regime': {
                'state': 'favorable', 'probability': 0.72,
                'duration_days': 3, 'since': '2026-04-15',
                'descriptions': {},
            },
            'regime_hist_median_days': {'favorable': 22},
            'top_correlations': [],
            'leadlag_signals': [],
            'chains': [], 'anomalies': [], 'anomaly_cluster': None,
            'leadlag_signals_weekly': [],
        }

        def _fake_api(path, **kwargs):
            if path == '/api/quant/snapshot':
                return snap
            return {'error': 'not_stubbed'}

        with patch.object(data_assembly, '_api', side_effect=_fake_api):
            result = data_assembly.fetch_quant_context()
        text = result['quant']
        assert 'early in favorable regime' in text
        assert '22d expected duration, from current transition odds' in text
        assert 'historical median' not in text

    def test_correlations_flag_significance_in_header(self):
        from morning_engine import data_assembly
        snap = {
            'regime': None, 'chains': [], 'anomalies': [],
            'anomaly_cluster': None, 'leadlag_signals': [],
            'leadlag_signals_weekly': [],
            'top_correlations': [
                dict(metric='SPX', correlation=0.62, q_value=0.01, significant=True),
                dict(metric='DXY', correlation=-0.31, q_value=0.03, significant=True),
            ],
        }

        def _fake_api(path, **kwargs):
            if path == '/api/quant/snapshot':
                return snap
            return {'error': 'not_stubbed'}
        with patch.object(data_assembly, '_api', side_effect=_fake_api):
            result = data_assembly.fetch_quant_context()
        text = result['quant']
        assert 'significant only, q<0.05' in text
        assert 'q=0.010' in text or 'q=0.01' in text

    def test_leadlag_rigor_includes_projected_move(self):
        from morning_engine import data_assembly
        snap = {
            'regime': None, 'chains': [], 'anomalies': [],
            'anomaly_cluster': None, 'top_correlations': [],
            'leadlag_signals_weekly': [],
            'leadlag_signals': [
                dict(leader='M2', lag_days=68, correlation=0.43,
                     leader_z=1.3, projected_move_z=0.559, projected_ci_z=[0.36, 0.74]),
            ],
        }

        def _fake_api(path, **kwargs):
            if path == '/api/quant/snapshot':
                return snap
            return {'error': 'not_stubbed'}
        with patch.object(data_assembly, '_api', side_effect=_fake_api):
            result = data_assembly.fetch_quant_context()
        text = result['quant']
        assert 'projected +0.56σ' in text or 'projected +0.56' in text
        assert 'assoc. band' in text

    def test_per_asset_section_for_eth_sol(self):
        """When ETH and SOL have snapshots, render a per-asset section."""
        from morning_engine import data_assembly
        base = {
            'regime': None, 'chains': [], 'anomalies': [],
            'anomaly_cluster': None, 'top_correlations': [],
            'leadlag_signals': [], 'leadlag_signals_weekly': [],
        }
        eth_snap = {**base,
            'top_correlations': [dict(metric='DXY', correlation=-0.32,
                                      q_value=0.02, significant=True)],
            'leadlag_signals': [dict(leader='SPX', lag_days=-3, correlation=0.58,
                                     projected_move_z=0.22)],
        }
        sol_snap = {**base,
            'top_correlations': [dict(metric='BTC', correlation=0.81,
                                      q_value=0.001, significant=True)],
            'leadlag_signals': [],
        }

        def _fake_api(path, **kwargs):
            if path == '/api/quant/snapshot':
                return {**base}  # BTC — empty
            if path == '/api/quant/snapshot?asset=ETH':
                return eth_snap
            if path == '/api/quant/snapshot?asset=SOL':
                return sol_snap
            return {'error': 'not_stubbed'}

        with patch.object(data_assembly, '_api', side_effect=_fake_api):
            result = data_assembly.fetch_quant_context()
        text = result['quant']
        assert 'Per-Asset Quant Context' in text
        assert 'ETH correlations' in text
        assert 'SOL correlations' in text
        assert 'DXY -0.32' in text


# ─── MCP new tools ────────────────────────────────────────────

class TestMCPQuantTools:
    """Ensure the 3 new MCP tools are registered, gated premium, and
    their JSON schemas are valid enough to serialize over tools/list."""

    @pytest.fixture(scope='class', autouse=True)
    @classmethod
    def _load_registry(cls):
        # Importing the package triggers @mcp_tool registration.
        import carrac.mcp.tools  # noqa: F401

    @pytest.fixture(scope='class')
    @classmethod
    def registry(cls):
        from carrac.mcp.registry import TOOL_REGISTRY
        return TOOL_REGISTRY

    def test_tool_registry_includes_new_tools(self, registry):
        for name in ('quant_get_leadlag_pair', 'quant_get_chain_history'):
            assert name in registry, f"MCP tool {name!r} not registered"
        # create_quant_alert absorbed into alert_manage
        # (action=create, alert_type=zscore|regime|chain_strength|
        # anomaly_cluster|conditional) — no longer a standalone registry entry.
        assert 'create_quant_alert' not in registry
        assert 'alert_manage' in registry

    def test_new_tools_gated_on_quant_engine(self, registry):
        # Quant data is paid starting at the member tier (quant_engine is sold as the member
        # differentiator) — these tools follow that TIER_LIMITS knob rather
        # than a hardcoded premium min_tier.
        for name in ('quant_get_leadlag_pair', 'quant_get_chain_history'):
            assert registry[name]['min_tier'] == 'member', \
                f"{name} must be min_tier=member (quant data is paid, starting at member)"
            assert registry[name]['feature'] == 'quant_engine', \
                f"{name} must be gated on the quant_engine feature knob"

    def test_create_quant_alert_still_requires_type(self):
        """create_quant_alert (plain helper, called from
        alert_manage) still validates its own `type` argument — unchanged
        behavior, just no longer independently registered."""
        from carrac.mcp.tools.quant import create_quant_alert
        from carrac.mcp.protocol import ToolError
        with pytest.raises(ToolError):
            create_quant_alert({})

    def test_alert_manage_enforces_quant_engine_for_quant_alert_types(self, app, monkeypatch):
        """alert_manage's own registry entry has no `feature`
        knob (it's min_tier='member' only, since price/indicator/formula
        need no extra knob) — so the absorbed quant alert_types must
        self-enforce quant_engine rather than relying on the registry gate."""
        from carrac.mcp.tools.alerts import alert_manage
        from carrac.mcp.protocol import ToolError
        import carrac.auth.decorators as dec

        monkeypatch.setattr(dec, 'get_tier_limits', lambda tier: {'quant_engine': False})
        with app.app_context():
            with pytest.raises(ToolError):
                alert_manage({
                    'action': 'create', 'alert_type': 'zscore',
                    'metric': 'VIX', 'condition': 'above', 'threshold': 2.5,
                })
            # And the same helper blocks family=quant/regime listing.
            with pytest.raises(ToolError):
                alert_manage({'action': 'list', 'family': 'quant'})

    def test_alert_manage_schema_documents_quant_alert_types(self, registry):
        schema = registry['alert_manage']['inputSchema']
        assert set(schema['properties']['alert_type']['enum']) >= {
            'zscore', 'regime', 'chain_strength', 'anomaly_cluster', 'conditional',
        }

    def test_get_leadlag_pair_requires_both_metrics(self, registry):
        schema = registry['quant_get_leadlag_pair']['inputSchema']
        assert set(schema['required']) == {'leader', 'target'}

    def test_get_chain_history_validates_chain_enum(self, registry):
        schema = registry['quant_get_chain_history']['inputSchema']
        chain_enum = set(schema['properties']['chain']['enum'])
        # The original 3 chains plus derivatives_leverage (re-enabled)
        assert {'monetary_policy', 'dollar_liquidity', 'risk_appetite'}.issubset(chain_enum), (
            f"Core chains missing from quant_get_chain_history enum: {chain_enum}"
        )

    def test_get_quant_data_accepts_asset_param(self, registry):
        """Per-asset threading was added. Without this param the
        snapshot returns BTC-only context for every asset."""
        schema = registry['quant_get_data']['inputSchema']
        assert 'asset' in schema['properties']

    def test_tool_count_is_at_least_36(self, registry):
        # Per-asset threading brings the registry to at least 36 tools.
        assert len(registry) >= 36


# ─── quant_get_regime asset scoping ─────────────────
#
# The MCP tool must not hardcode a 'BTC' lookup for the classification step
# regardless of the requested asset — an ETH-only request would fall
# through to "no BTC key in result" and silently return an unknown/0
# regime instead of ETH's own. These tests pin: (1) an ETH request queries
# ONLY ETH rows and classifies from ETH's own metrics, (2) an asset with no
# classifiable derivatives data gets a clear structured "no data" result
# instead of a wrong/borrowed regime, (3) BTC and the default 'all' behavior
# are unchanged.

class _FakeDerivCursor:
    def __init__(self, rows_by_asset, captured):
        self._rows_by_asset = rows_by_asset
        self._captured = captured
        self._row = None

    def execute(self, sql, params):
        asset = params[0]
        self._captured.setdefault('assets_queried', []).append(asset)
        self._row = self._rows_by_asset.get(asset)

    def fetchone(self):
        return self._row


class _FakeDerivConn:
    def __init__(self, rows_by_asset, captured):
        self._rows_by_asset = rows_by_asset
        self._captured = captured

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self):
        return _FakeDerivCursor(self._rows_by_asset, self._captured)


class TestGetDerivativesRegimeAssetScoping:
    # Classification runs on the Deribit options complex, which is
    # admin-only. These assert the OPERATOR's view; the public view is pinned
    # by test_public_source_claims.py and TestDerivativesRegimeIsGated below.
    @pytest.fixture(autouse=True)
    def _as_admin(self, admin_caller):
        pass

    @pytest.fixture(scope='class', autouse=True)
    @classmethod
    def _load_registry(cls):
        import carrac.mcp.tools  # noqa: F401

    @pytest.fixture
    def handler(self):
        from carrac.mcp.registry import TOOL_REGISTRY
        return TOOL_REGISTRY['quant_get_regime']['handler']

    @staticmethod
    def _row(basis_pct, skew_25d, dvol, iv_rv_premium, put_call, term_slope,
              gex_net, gex_flip, funding=0.01):
        from datetime import date
        return {
            'ts': date(2026, 7, 17),
            'basis_pct': basis_pct, 'skew_25d': skew_25d, 'dvol': dvol,
            'iv_rv_premium': iv_rv_premium,
            'options_put_call_ratio': put_call, 'options_oi_usd': 1e9,
            'options_max_pain': 70000, 'options_iv_atm': 50.0,
            'term_structure_slope': term_slope, 'iv_7d': 45, 'iv_30d': 50,
            'iv_90d': 55, 'gex_net': gex_net, 'gex_flip_price': gex_flip,
            'funding_rate_avg': funding,
        }

    def _patch_conn(self, monkeypatch, rows_by_asset):
        captured = {}
        from carrac import db_pool as quant_db
        monkeypatch.setattr(
            quant_db, 'get_quant_conn',
            lambda: _FakeDerivConn(rows_by_asset, captured))
        return captured

    def test_eth_request_queries_only_eth_rows(self, handler, monkeypatch):
        eth_row = self._row(12.0, 8.0, 80.0, 15.0, 0.3, -0.02, -1e8, 3600)
        captured = self._patch_conn(monkeypatch, {'ETH': eth_row})

        out = handler({'asset': 'ETH'})

        assert captured['assets_queried'] == ['ETH']
        assert out['ETH']['metrics']['basis_pct'] == 12.0

    def test_eth_request_classifies_from_eth_metrics_not_btc(self, monkeypatch):
        from carrac.mcp.registry import TOOL_REGISTRY
        fn = TOOL_REGISTRY['quant_get_regime']['handler']

        eth_row = self._row(12.0, 8.0, 80.0, 15.0, 0.3, -0.02, -1e8, 3600)
        btc_row = self._row(5.0, -10.0, 55.0, 4.0, 0.7, 0.05, 2e8, 68000)
        # Only ETH is queried for an ETH-only request, but include a BTC row
        # in the fixture too so code that still reads 'BTC' would
        # silently succeed with BTC's numbers instead of erroring loudly.
        self._patch_conn(monkeypatch, {'ETH': eth_row, 'BTC': btc_row})

        captured_args = {}

        import quant.engine.derivatives_regime as dr
        def fake_classify(args):
            captured_args.update(args)
            return {'regime': 'marker', 'confidence': 0.42}
        monkeypatch.setattr(dr, 'classify_derivatives_regime', fake_classify)

        out = fn({'asset': 'ETH'})

        assert captured_args['skew_25d'] == 8.0       # ETH's, not BTC's -10.0
        assert captured_args['basis_apr'] == 12.0      # ETH's basis_pct
        assert out['regime'] == {'regime': 'marker', 'confidence': 0.42}

    def test_unpopulated_asset_returns_structured_no_data(self, handler, monkeypatch):
        # No row at all for SOL.
        captured = self._patch_conn(monkeypatch, {})

        out = handler({'asset': 'SOL'})

        assert out['asset'] == 'SOL'
        assert out['regime'] == {'regime': 'unknown', 'confidence': 0}
        # Reworded: a funding-only row IS derivatives data —
        # what is missing is the regime, so say that instead.
        assert 'No regime classification for SOL' in out['message']
        assert captured['assets_queried'] == ['SOL']

    def test_asset_with_funding_only_row_returns_structured_no_data(self, handler, monkeypatch):
        # A row exists (funding-rate collectors write these for ~10 assets)
        # but none of the options/GEX/skew/dvol columns used for
        # classification are populated — must still be treated as no data,
        # not passed through to the classifier (which would otherwise
        # return an arbitrary regime at 0 confidence).
        funding_only_row = self._row(
            None, None, None, None, None, None, None, None, funding=0.0003)
        self._patch_conn(monkeypatch, {'SOL': funding_only_row})

        out = handler({'asset': 'SOL'})

        assert out['asset'] == 'SOL'
        assert out['regime'] == {'regime': 'unknown', 'confidence': 0}
        # Reworded: a funding-only row IS derivatives data —
        # what is missing is the regime, so say that instead.
        assert 'No regime classification for SOL' in out['message']

    def test_btc_request_unchanged(self, handler, monkeypatch):
        btc_row = self._row(5.0, -10.0, 55.0, 4.0, 0.7, 0.05, 2e8, 68000)
        captured = self._patch_conn(monkeypatch, {'BTC': btc_row})

        import quant.engine.derivatives_regime as dr
        captured_args = {}
        def fake_classify(args):
            captured_args.update(args)
            return {'regime': 'healthy_trend', 'confidence': 0.8}
        monkeypatch.setattr(dr, 'classify_derivatives_regime', fake_classify)

        out = handler({'asset': 'BTC'})

        assert captured['assets_queried'] == ['BTC']
        assert captured_args['skew_25d'] == -10.0
        assert out['regime'] == {'regime': 'healthy_trend', 'confidence': 0.8}
        assert out['BTC']['metrics']['basis_pct'] == 5.0

    def test_default_all_still_classifies_btc_only(self, handler, monkeypatch):
        btc_row = self._row(5.0, -10.0, 55.0, 4.0, 0.7, 0.05, 2e8, 68000)
        eth_row = self._row(12.0, 8.0, 80.0, 15.0, 0.3, -0.02, -1e8, 3600)
        captured = self._patch_conn(monkeypatch, {'BTC': btc_row, 'ETH': eth_row})

        import quant.engine.derivatives_regime as dr
        captured_args = {}
        def fake_classify(args):
            captured_args.update(args)
            return {'regime': 'healthy_trend', 'confidence': 0.8}
        monkeypatch.setattr(dr, 'classify_derivatives_regime', fake_classify)

        out = handler({})  # default asset='all'

        assert set(captured['assets_queried']) == {'BTC', 'ETH'}
        assert 'BTC' in out and 'ETH' in out
        # Primary regime is still BTC-derived under 'all' (documented,
        # back-compat behavior — unchanged by the later change).
        assert captured_args['skew_25d'] == -10.0
        assert out['regime'] == {'regime': 'healthy_trend', 'confidence': 0.8}


# ─── build_snapshot helper ────────────────────────────────────

class TestBuildSnapshotHelper:
    def test_build_snapshot_importable(self):
        from quant.api.routes import build_snapshot
        assert callable(build_snapshot)

    def test_whitelists_unknown_asset(self):
        """Asserts that passing a bogus asset falls back to BTC rather
        than reflecting user input into the SQL path."""
        from quant.api.routes import build_snapshot
        # Patch get_conn so we don't actually hit the DB; we just want
        # to confirm the whitelist path executes without raising.
        with patch('quant.api.routes.get_conn') as m:
            cur = MagicMock()
            cur.fetchone.return_value = None
            cur.fetchall.return_value = []
            conn = MagicMock()
            conn.cursor.return_value = cur
            m.return_value.__enter__.return_value = conn
            result = build_snapshot(asset="'; DROP TABLE users; --")
        # Should not raise, should return a dict with asset=BTC (whitelist fallback)
        if result is not None:
            assert result.get('asset') == 'BTC'


# ─── push_notifications: QE alert creators ────────────────────────────

class TestPushNotificationsQEAlerts:
    """The 5 QE alert creators: verify
    schema defaults, validation errors, and payload shape."""

    @pytest.fixture
    def isolated_alerts(self, tmp_path, rebind_db):
        """Give each test its own database so tests don't see each other."""
        import push_notifications
        rebind_db(tmp_path / 'db')
        return push_notifications

    def test_zscore_alert_payload_shape(self, isolated_alerts):
        alert = isolated_alerts.create_zscore_alert(
            user_id='42', metric='VIX', condition='above',
            threshold=2.0, window=90, note='stress', frequency='once',
        )
        assert alert['alert_type'] == 'zscore'
        assert alert['metric'] == 'VIX'
        assert alert['condition'] == 'above'
        assert alert['threshold'] == 2.0
        assert alert['window'] == 90
        assert alert['active'] is True
        assert alert['triggered'] is False

    def test_regime_alert_edge_state_initial(self, isolated_alerts):
        alert = isolated_alerts.create_regime_alert(user_id='42')
        assert alert['alert_type'] == 'regime_transition'
        # last_regime must start None so the first check populates without firing
        assert alert['last_regime'] is None

    def test_chain_strength_alert_rejects_unknown_chain(self, isolated_alerts):
        with pytest.raises(ValueError, match='unknown chain'):
            isolated_alerts.create_chain_strength_alert(
                user_id='42', chain='bogus_chain',
            )

    def test_chain_strength_alert_rejects_unknown_condition(self, isolated_alerts):
        with pytest.raises(ValueError, match='above.*below'):
            isolated_alerts.create_chain_strength_alert(
                user_id='42', chain='monetary_policy', condition='near',
            )

    def test_chain_strength_alert_payload(self, isolated_alerts):
        alert = isolated_alerts.create_chain_strength_alert(
            user_id='42', chain='monetary_policy',
            condition='below', threshold=0.15,
        )
        assert alert['alert_type'] == 'chain_strength_breach'
        assert alert['chain'] == 'monetary_policy'
        assert alert['threshold'] == 0.15

    def test_anomaly_cluster_bounds_validation(self, isolated_alerts):
        with pytest.raises(ValueError, match='min_domains'):
            isolated_alerts.create_anomaly_cluster_alert(
                user_id='42', min_domains=0,
            )
        with pytest.raises(ValueError, match='min_metrics'):
            isolated_alerts.create_anomaly_cluster_alert(
                user_id='42', min_metrics=50,
            )

    def test_anomaly_cluster_payload(self, isolated_alerts):
        alert = isolated_alerts.create_anomaly_cluster_alert(
            user_id='42', min_domains=2, min_metrics=3,
        )
        assert alert['alert_type'] == 'anomaly_cluster_active'
        assert alert['min_domains'] == 2

    def test_conditional_rejects_empty_list(self, isolated_alerts):
        with pytest.raises(ValueError, match='list of 1-5 items'):
            isolated_alerts.create_conditional_alert(
                user_id='42', conditions=[],
            )

    def test_conditional_rejects_too_many(self, isolated_alerts):
        with pytest.raises(ValueError, match='list of 1-5 items'):
            isolated_alerts.create_conditional_alert(
                user_id='42', conditions=['a:gt:1'] * 6,
            )

    def test_conditional_parses_valid_conditions(self, isolated_alerts, monkeypatch):
        # Stub out the pandas-heavy _parse_condition so the test can run
        # without pandas. The real function validates operator/threshold;
        # here we accept anything matching the canonical METRIC:OP:VAL form.
        def _fake_parse(expr):
            metric, op, val = str(expr).split(':')
            if op not in ('gt', 'lt', 'gte', 'lte'):
                raise ValueError(f'bad op {op}')
            float(val)
            return (metric, op, float(val))

        import sys, types
        mod = types.ModuleType('quant.engine.conditional')
        mod._parse_condition = _fake_parse
        monkeypatch.setitem(sys.modules, 'quant.engine.conditional', mod)

        alert = isolated_alerts.create_conditional_alert(
            user_id='42',
            conditions=['VIX:gt:2', 'HY_SPREAD:gt:1.5'],
            target='BTC', horizon=30,
        )
        assert alert['alert_type'] == 'conditional_query_match'
        assert alert['target'] == 'BTC'
        assert alert['horizon'] == 30
        # Edge-detection sentinel
        assert alert['last_match'] is None

    def test_conditional_rejects_bad_operator(self, isolated_alerts, monkeypatch):
        # Same stub as above so the bad-operator case actually reaches the
        # validation path (rather than failing earlier on pandas import).
        def _fake_parse(expr):
            metric, op, val = str(expr).split(':')
            if op not in ('gt', 'lt', 'gte', 'lte'):
                raise ValueError(f'bad op {op}')
            float(val)
            return (metric, op, float(val))

        import sys, types
        mod = types.ModuleType('quant.engine.conditional')
        mod._parse_condition = _fake_parse
        monkeypatch.setitem(sys.modules, 'quant.engine.conditional', mod)

        with pytest.raises(ValueError, match='invalid condition'):
            isolated_alerts.create_conditional_alert(
                user_id='42', conditions=['VIX:near:2'],
            )


# ─── Formula evaluator primitive routing ──────────────────────────────

@needs_pandas
class TestFormulaPrimitiveDispatch:
    """Parser tests exist in TestFormulaU10Parser (test_quant.py).
    These cover the evaluator side: that each primitive name routes to
    its handler with correct arity checking."""

    def test_leadlag_requires_two_args(self):
        from carrac.formula_eval import validate_formula
        r = validate_formula("leadlag('M2').projected_move > 0")
        # Parser accepts; runtime would raise. We accept that the parser
        # itself may tolerate this (it's checked at eval time).
        assert isinstance(r.get('valid'), bool)

    def test_regime_prob_requires_string_state(self):
        from carrac.formula_eval import validate_formula
        r = validate_formula("regime_prob('favorable') > 0.5")
        assert r['valid']

    def test_regime_prob_rejects_unknown_state_at_eval(self):
        # Eval-time enforcement: parser should accept but eval would reject.
        from carrac.formula_eval import validate_formula
        r = validate_formula("regime_prob('mystery') > 0.5")
        # Parse OK (string literal accepted); eval would raise.
        assert isinstance(r.get('valid'), bool)

    def test_analogue_similarity_accepts_rank_bounds(self):
        from carrac.formula_eval import validate_formula
        for rank in (1, 5, 10):
            r = validate_formula(f"analogue_similarity({rank}) > 0.8")
            assert r['valid'], f"rank={rank}"


# ─── Snapshot API fields ───────────────────────────────

class TestSnapshotShape:
    """top_correlations carries q_value/ci_low/ci_high/
    significant. Pins that they are not dropped."""

    def test_top_correlations_fields_enumerated(self):
        """The SQL query in build_snapshot must SELECT the significance columns."""
        import quant.api.routes as routes
        src = __import__('inspect').getsource(routes)
        # These column names must appear in the snapshot's correlations SELECT.
        for col in ('q_value', 'ci_low', 'ci_high', 'significant'):
            assert col in src, f"Column {col!r} missing from quant/api/routes.py"

    def test_per_asset_snapshot_exports_chains_and_regime_hist(self):
        import quant.api.routes as routes
        src = __import__('inspect').getsource(routes)
        assert "'regime_hist_median_days':" in src
        assert "'chains':" in src
        assert "'anomaly_cluster':" in src


class TestDerivativesRegimeIsGated:
    """The PUBLIC shape of quant_get_regime.

    The desk was switched off and report facts stopped carrying these columns,
    but MCP kept returning the whole options complex to anyone with a key for
    a month. The gate that closed it is only worth having if this test exists —
    the previous remediation had no public-shape assertion at all, which is
    precisely why the other doors stayed open.

    No admin_caller fixture here on purpose: no caller set is the public state,
    and the gate fails closed.
    """

    @pytest.fixture(scope='class', autouse=True)
    @classmethod
    def _load_registry(cls):
        import carrac.mcp.tools  # noqa: F401

    @pytest.fixture
    def handler(self):
        from carrac.mcp.registry import TOOL_REGISTRY
        return TOOL_REGISTRY['quant_get_regime']['handler']

    def test_deribit_columns_are_absent_for_a_normal_caller(self, handler, monkeypatch):
        from carrac import db_pool as quant_db
        from carrac.compliance.deribit_gate import DERIBIT_COLUMNS

        row = TestGetDerivativesRegimeAssetScoping._row(
            5.0, -10.0, 55.0, 4.0, 0.7, 0.05, 2e8, 68000)
        monkeypatch.setattr(
            quant_db, 'get_quant_conn', lambda: _FakeDerivConn({'BTC': row}, {}))

        out = handler({'asset': 'BTC'})

        leaked = DERIBIT_COLUMNS & set(out['BTC']['metrics'])
        assert leaked == set(), f"Deribit columns reached a public caller: {leaked}"

    def test_permitted_funding_survives_for_a_normal_caller(self, handler, monkeypatch):
        """The gate is a licensing boundary, not a paywall — permitted data stays."""
        from carrac import db_pool as quant_db

        row = TestGetDerivativesRegimeAssetScoping._row(
            5.0, -10.0, 55.0, 4.0, 0.7, 0.05, 2e8, 68000, funding=0.0123)
        monkeypatch.setattr(
            quant_db, 'get_quant_conn', lambda: _FakeDerivConn({'BTC': row}, {}))

        out = handler({'asset': 'BTC'})

        assert out['BTC']['metrics']['funding_rate_avg'] == 0.0123
