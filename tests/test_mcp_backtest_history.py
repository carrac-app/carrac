"""Tests for backtest run history.

Migration tests verify the revision file's structure.
Tool tests exercise save/list/get using SQLite (no Postgres needed).
Tier-gating tests verify the `backtester_save` knob enforcement (these 3 tools must not hardcode min_tier=premium, which would exclude the member tier
even though TIER_LIMITS['member']['backtester_save'] is True).
"""

import pytest

from carrac.mcp import tools  # noqa: F401 — ensures registration
from carrac.mcp.registry import TOOL_REGISTRY, tool_entitled
from carrac.mcp.protocol import ToolError
from carrac.mcp.backtest_models import MODEL_HOLD_N, MODEL_FULL_SIMULATION
from carrac.mcp.tools.backtests import list_backtest_runs, get_backtest_run


# ── Helpers ────────────────────────────────────────────────────
#
# list_backtest_runs/get_backtest_run are plain helper functions
# now, absorbed into backtest_get_runs (discriminated by run_id presence) —
# no longer separate TOOL_REGISTRY entries. Called directly here.

SAVE_FN = lambda: TOOL_REGISTRY['backtest_save_run']['handler']  # noqa: E731
LIST_FN = lambda: list_backtest_runs  # noqa: E731
GET_FN  = lambda: get_backtest_run   # noqa: E731

_GOOD_BT_RESULT = {
    'model': MODEL_HOLD_N,
    'symbol': 'BTC',
    'expression': 'RSI(14) < 30',
    'timeframe': '1D',
    'lookback_bars': 365,
    'hold_bars': 5,
    'trades': 3,
    'hit_rate': 0.6667,
    'mean_return': 0.0423,
    'median_return': 0.0310,
    'stdev_return': 0.0180,
    'sharpe_like': 2.35,
    'max_drawdown': -0.04,
    'final_equity_multiple': 1.13,
}


def _mock_backtest(monkeypatch, result=None):
    """Patch the module-level _run_backtest to return a fixed result."""
    payload = result if result is not None else dict(_GOOD_BT_RESULT)

    monkeypatch.setattr(
        'carrac.mcp.tools.backtests._run_backtest',
        lambda _args: payload,
    )


# ── Migration file structure ───────────────────────────────

def _load_migration():
    """Load the backtest_runs migration module by file path."""
    import importlib.util
    import os
    migration_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        'alembic', 'versions', '20260511_backtest_runs.py',
    )
    spec = importlib.util.spec_from_file_location('migration_backtest_runs', migration_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestMigrationFileStructure:
    """Validates the Alembic revision file without running DDL."""

    def test_migration_module_importable(self):
        mod = _load_migration()
        assert mod is not None

    def test_revision_id(self):
        mod = _load_migration()
        assert mod.revision == 't0u1v2w3x4y5'

    def test_down_revision_matches_head(self):
        mod = _load_migration()
        # Must chain from the previous head revision.
        assert mod.down_revision == 's9t0u1v2w3x4'

    def test_upgrade_callable(self):
        mod = _load_migration()
        assert callable(mod.upgrade)

    def test_downgrade_callable(self):
        mod = _load_migration()
        assert callable(mod.downgrade)

    def test_upgrade_uses_create_table_if_not_exists(self):
        """Idempotency: upgrade uses IF NOT EXISTS so re-running is a no-op."""
        import inspect
        mod = _load_migration()
        src = inspect.getsource(mod.upgrade)
        assert 'CREATE TABLE IF NOT EXISTS' in src
        assert 'CREATE INDEX IF NOT EXISTS' in src

    def test_downgrade_drops_table(self):
        """Downgrade removes the table."""
        import inspect
        mod = _load_migration()
        src = inspect.getsource(mod.downgrade)
        assert 'DROP TABLE IF EXISTS' in src


# ── Tool registration ──────────────────────────────────────

class TestToolRegistration:
    """list_backtest_runs + get_backtest_run consolidated into
    backtest_get_runs (run_id present -> get, absent -> list)."""

    def test_two_tools_registered_not_old_names(self):
        for name in ('backtest_save_run', 'backtest_get_runs'):
            assert name in TOOL_REGISTRY, f'{name} not registered'
        for name in ('list_backtest_runs', 'get_backtest_run'):
            assert name not in TOOL_REGISTRY, f'{name} should no longer be a standalone tool'

    def test_all_tools_gated_on_backtester_save_feature(self):
        """Member is entitled (backtester_save=True) — these tools
        must key off that TIER_LIMITS knob rather than a hardcoded min_tier."""
        for name in ('backtest_save_run', 'backtest_get_runs'):
            assert TOOL_REGISTRY[name]['min_tier'] == 'member', (
                f'{name} must be min_tier=member (backtester_save is True for member)'
            )
            assert TOOL_REGISTRY[name]['feature'] == 'backtester_save', (
                f'{name} must be gated on the backtester_save feature knob'
            )

    def test_legacy_names_dispatch_to_backtest_get_runs(self):
        from carrac.mcp.registry import resolve_tool_name
        assert resolve_tool_name('list_backtest_runs') == 'backtest_get_runs'
        assert resolve_tool_name('get_backtest_run') == 'backtest_get_runs'

    def test_backtest_get_runs_dispatches_on_run_id(self, app, bind_mcp_user, monkeypatch):
        _mock_backtest(monkeypatch)
        from carrac.mcp.tools.backtests import backtest_get_runs
        with app.app_context():
            saved = SAVE_FN()({'symbol': 'BTC', 'expression': 'RSI(14) < 30'})
            listed = backtest_get_runs({})
            got = backtest_get_runs({'run_id': saved['run_id']})
        assert 'runs' in listed and 'run_id' not in listed
        assert got['run_id'] == saved['run_id']


# ── backtest_save_run ──────────────────────────────────────

class TestSaveBacktestRun:
    def test_saves_and_returns_run_id(self, app, bind_mcp_user, monkeypatch):
        _mock_backtest(monkeypatch)
        with app.app_context():
            out = SAVE_FN()({
                'symbol': 'BTC',
                'expression': 'RSI(14) < 30',
                'timeframe': '1D',
                'lookback_bars': 365,
                'hold_bars': 5,
                'name': 'Low RSI buy',
            })
        assert isinstance(out['run_id'], int)
        assert out['run_id'] > 0
        assert out['name'] == 'Low RSI buy'
        assert out['symbol'] == 'BTC'
        assert 'result' in out

    def test_missing_symbol_raises(self, app, bind_mcp_user, monkeypatch):
        _mock_backtest(monkeypatch)
        with app.app_context():
            with pytest.raises(ToolError):
                SAVE_FN()({'expression': 'RSI(14) < 30'})

    def test_missing_expression_raises(self, app, bind_mcp_user, monkeypatch):
        _mock_backtest(monkeypatch)
        with app.app_context():
            with pytest.raises(ToolError):
                SAVE_FN()({'symbol': 'BTC'})

    def test_backtest_error_propagates(self, app, bind_mcp_user, monkeypatch):
        monkeypatch.setattr(
            'carrac.mcp.tools.backtests._run_backtest',
            lambda _: {'error': 'No OHLCV data for XYZ/1D'},
        )
        with app.app_context():
            with pytest.raises(ToolError, match='Backtest failed'):
                SAVE_FN()({'symbol': 'XYZ', 'expression': 'RSI(14) > 0'})

    def test_anonymous_name_is_null(self, app, bind_mcp_user, monkeypatch):
        _mock_backtest(monkeypatch)
        with app.app_context():
            out = SAVE_FN()({
                'symbol': 'ETH',
                'expression': 'RSI(14) > 70',
            })
        assert out['name'] is None


# ── list_backtest_runs ─────────────────────────────────────

class TestListBacktestRuns:
    def test_returns_saved_run(self, app, bind_mcp_user, monkeypatch):
        _mock_backtest(monkeypatch)
        with app.app_context():
            save = SAVE_FN()({
                'symbol': 'BTC', 'expression': 'RSI(14) < 30', 'name': 'test-list'
            })
            lst = LIST_FN()({})

        assert lst['count'] >= 1
        ids = [r['run_id'] for r in lst['runs']]
        assert save['run_id'] in ids

    def test_symbol_filter_isolates_results(self, app, bind_mcp_user, monkeypatch):
        _mock_backtest(monkeypatch)
        with app.app_context():
            SAVE_FN()({'symbol': 'BTC', 'expression': 'RSI(14) < 30'})
            SAVE_FN()({'symbol': 'ETH', 'expression': 'RSI(14) < 40'})
            lst = LIST_FN()({'symbol': 'ETH'})

        assert all(r['symbol'] == 'ETH' for r in lst['runs'])

    def test_limit_respected(self, app, bind_mcp_user, monkeypatch):
        _mock_backtest(monkeypatch)
        with app.app_context():
            for _ in range(5):
                SAVE_FN()({'symbol': 'BTC', 'expression': 'RSI(14) < 30'})
            lst = LIST_FN()({'limit': 2})

        assert lst['count'] <= 2
        assert len(lst['runs']) <= 2

    def test_summary_fields_present(self, app, bind_mcp_user, monkeypatch):
        _mock_backtest(monkeypatch)
        with app.app_context():
            SAVE_FN()({'symbol': 'BTC', 'expression': 'RSI(14) < 30'})
            lst = LIST_FN()({})

        run = lst['runs'][0]
        for field in ('run_id', 'symbol', 'expression', 'timeframe',
                      'hit_rate', 'mean_return', 'trades', 'created_at'):
            assert field in run, f'Missing field: {field}'

    def test_no_full_result_blob_in_list(self, app, bind_mcp_user, monkeypatch):
        """list_backtest_runs must NOT return the full result JSONB."""
        _mock_backtest(monkeypatch)
        with app.app_context():
            SAVE_FN()({'symbol': 'BTC', 'expression': 'RSI(14) < 30'})
            lst = LIST_FN()({})

        run = lst['runs'][0]
        assert 'result' not in run


# ── get_backtest_run ───────────────────────────────────────

class TestGetBacktestRun:
    def test_returns_full_result(self, app, bind_mcp_user, monkeypatch):
        _mock_backtest(monkeypatch)
        with app.app_context():
            saved = SAVE_FN()({
                'symbol': 'BTC', 'expression': 'RSI(14) < 30', 'name': 'full-check'
            })
            got = GET_FN()({'run_id': saved['run_id']})

        assert got['run_id'] == saved['run_id']
        assert got['symbol'] == 'BTC'
        assert got['name'] == 'full-check'
        assert 'result' in got
        assert isinstance(got['result'], dict)
        assert 'hit_rate' in got['result']

    def test_missing_run_id_raises(self, app, bind_mcp_user, monkeypatch):
        with app.app_context():
            with pytest.raises((ToolError, TypeError, ValueError)):
                GET_FN()({})

    def test_not_found_raises(self, app, bind_mcp_user, monkeypatch):
        with app.app_context():
            with pytest.raises(ToolError, match='not found'):
                GET_FN()({'run_id': 999999})

    def test_other_users_run_not_accessible(self, app, bind_mcp_user, monkeypatch):
        """A run owned by user 42 must not be accessible to user 99."""
        _mock_backtest(monkeypatch)
        with app.app_context():
            # Save as user 42 (default bind)
            saved = SAVE_FN()({'symbol': 'BTC', 'expression': 'RSI(14) < 30'})
            run_id = saved['run_id']

            # Switch to user 99
            bind_mcp_user(uid=99)
            with pytest.raises(ToolError, match='not found'):
                GET_FN()({'run_id': run_id})


# ── Model field on save/list/get ──────────────
#
# All three tools only ever persist/retrieve Hold-N results (_run_backtest is
# a thin proxy to backtest_indicator_formula), so every response — including
# the per-run summaries in list_backtest_runs — must carry
# 'model': 'Hold-N Signal Test' at the top level.

class TestModelField:
    def test_save_backtest_run_carries_model(self, app, bind_mcp_user, monkeypatch):
        _mock_backtest(monkeypatch)
        with app.app_context():
            out = SAVE_FN()({'symbol': 'BTC', 'expression': 'RSI(14) < 30'})
        assert out['model'] == MODEL_HOLD_N == 'Hold-N Signal Test'
        assert out['result']['model'] == MODEL_HOLD_N  # nested result too

    def test_list_backtest_runs_carries_model(self, app, bind_mcp_user, monkeypatch):
        """`model` moved from the envelope onto each row.

        A top-level model was only ever true because Hold-N was the sole thing
        that could be persisted. Now that Full Simulation saves here too, one
        model name for the whole list would be a lie — so each row states its
        own, and `models` names the distinct set present.
        """
        _mock_backtest(monkeypatch)
        with app.app_context():
            SAVE_FN()({'symbol': 'BTC', 'expression': 'RSI(14) < 30'})
            lst = LIST_FN()({})
        assert lst['models'] == [MODEL_HOLD_N]
        assert all(r['model'] == MODEL_HOLD_N for r in lst['runs'])

    def test_get_backtest_run_carries_model(self, app, bind_mcp_user, monkeypatch):
        _mock_backtest(monkeypatch)
        with app.app_context():
            saved = SAVE_FN()({'symbol': 'BTC', 'expression': 'RSI(14) < 30'})
            got = GET_FN()({'run_id': saved['run_id']})
        assert got['model'] == MODEL_HOLD_N

    def test_run_backtest_proxy_passes_model_through(self, app, bind_mcp_user, monkeypatch):
        """The module-level _run_backtest proxy must not strip the field
        backtest_indicator_formula produces."""
        from carrac.mcp.tools import backtests as backtests_mod
        from carrac.mcp.tools import indicators as ind_mod

        captured = {}

        def fake_bt(args):
            out = {'model': MODEL_HOLD_N, 'symbol': args['symbol'], 'trades': 1,
                   'hit_rate': 1.0, 'mean_return': 0.01}
            captured['out'] = out
            return out

        monkeypatch.setattr(ind_mod, 'backtest_indicator_formula', fake_bt)
        result = backtests_mod._run_backtest({'symbol': 'BTC', 'expression': 'RSI(14) < 30'})
        assert result is captured['out']
        assert result['model'] == MODEL_HOLD_N


# ── Tier gating ───────────────────────────────────────────
#
# Gating now follows the `backtester_save` TIER_LIMITS knob
# (True for member/tester/premium/admin, False for free) via
# registry.tool_entitled(), not a hardcoded min_tier rank.

class TestTierGating:
    @pytest.mark.parametrize('name', ['backtest_save_run', 'backtest_get_runs'])
    def test_free_tier_blocked(self, name):
        reg = TOOL_REGISTRY[name]
        assert not tool_entitled('free', reg['min_tier'], reg['feature'])

    @pytest.mark.parametrize('name', ['backtest_save_run', 'backtest_get_runs'])
    @pytest.mark.parametrize('tier', ['member', 'tester', 'premium', 'admin'])
    def test_paid_tiers_allowed(self, name, tier):
        reg = TOOL_REGISTRY[name]
        assert tool_entitled(tier, reg['min_tier'], reg['feature']), (
            f'{tier} should be entitled to {name} (backtester_save)'
        )


# ── The two doors see the same data ──────────────────────────────
#
# This is the first cross-surface visibility test in the codebase. Every other
# feature is tested one surface at a time on the assumption that both call the
# same function — an assumption that was false here for backtests, silently, in
# both directions. These tests assert the round trip itself.

_STRATEGY_TOOL = 'backtest_manage_strategies'


class TestBacktestRunsVisibleInBothDoors:
    """A run saved over MCP must be readable from the web routes, and back."""

    def test_mcp_saved_run_is_listed_by_the_web_route(self, app, auth_client,
                                                      bind_mcp_user, monkeypatch):
        bind_mcp_user(uid='test-user')  # same identity auth_client logs in as
        _mock_backtest(monkeypatch)
        with app.app_context():
            saved = SAVE_FN()({'symbol': 'BTC', 'expression': 'RSI(14) < 30',
                               'name': 'agent run'})

        resp = auth_client.get('/api/backtest/runs')
        assert resp.status_code == 200
        runs = resp.get_json()['runs']
        assert [r['run_id'] for r in runs] == [saved['run_id']]
        assert runs[0]['name'] == 'agent run'
        assert runs[0]['model'] == MODEL_HOLD_N

    def test_mcp_saved_run_detail_is_readable_by_the_web_route(
            self, app, auth_client, bind_mcp_user, monkeypatch):
        bind_mcp_user(uid='test-user')
        _mock_backtest(monkeypatch)
        with app.app_context():
            saved = SAVE_FN()({'symbol': 'BTC', 'expression': 'RSI(14) < 30'})

        resp = auth_client.get(f'/api/backtest/runs/{saved["run_id"]}')
        assert resp.status_code == 200
        body = resp.get_json()
        assert body['result']['hit_rate'] == _GOOD_BT_RESULT['hit_rate']

    def test_another_users_run_is_not_readable(self, app, auth_client,
                                               bind_mcp_user, monkeypatch):
        bind_mcp_user(uid='someone-else')
        _mock_backtest(monkeypatch)
        with app.app_context():
            saved = SAVE_FN()({'symbol': 'BTC', 'expression': 'RSI(14) < 30'})

        assert auth_client.get('/api/backtest/runs').get_json()['runs'] == []
        assert auth_client.get(f'/api/backtest/runs/{saved["run_id"]}').status_code == 404

    def test_web_route_reports_the_save_response_pointer(self, app, bind_mcp_user,
                                                         monkeypatch):
        """backtest_save_run now tells the agent where the human can find it."""
        _mock_backtest(monkeypatch)
        with app.app_context():
            saved = SAVE_FN()({'symbol': 'BTC', 'expression': 'RSI(14) < 30'})
        assert 'History' in saved['visible_in_app']


class TestStrategiesVisibleInBothDoors:
    """The saved-strategy library was browser-only until the model field moved onto each row."""

    @staticmethod
    def _tool(args):
        return TOOL_REGISTRY[_STRATEGY_TOOL]['handler'](args)

    def test_browser_saved_strategy_is_visible_to_an_agent(self, app, auth_client,
                                                           bind_mcp_user):
        bind_mcp_user(uid='test-user')
        resp = auth_client.post('/api/strategies', json={
            'name': 'human recipe',
            'entry_formula': 'CROSSOVER(EMA(9), EMA(21))',
            'exit_formula': 'CROSSUNDER(EMA(9), EMA(21))',
        })
        assert resp.status_code == 201
        assert resp.get_json()['id']  # the id is now returned, not swallowed

        with app.app_context():
            out = self._tool({'action': 'list'})
        assert [s['name'] for s in out['strategies']] == ['human recipe']

    def test_agent_saved_strategy_is_visible_in_the_browser(self, app, auth_client,
                                                            bind_mcp_user):
        bind_mcp_user(uid='test-user')
        with app.app_context():
            saved = self._tool({
                'action': 'save',
                'name': 'agent recipe',
                'entry_formula': 'RSI(14) < 30',
                'exit_formula': 'RSI(14) > 70',
            })
        assert saved['id']
        assert 'Load' in saved['visible_in_app']

        listed = auth_client.get('/api/strategies').get_json()['strategies']
        assert [s['name'] for s in listed] == ['agent recipe']

    def test_agent_can_delete_what_the_browser_saved(self, app, auth_client,
                                                     bind_mcp_user):
        bind_mcp_user(uid='test-user')
        auth_client.post('/api/strategies', json={
            'name': 'doomed', 'entry_formula': 'A > B', 'exit_formula': 'A < B',
        })
        sid = auth_client.get('/api/strategies').get_json()['strategies'][0]['id']
        with app.app_context():
            assert self._tool({'action': 'delete', 'id': sid})['deleted'] == sid
        assert auth_client.get('/api/strategies').get_json()['strategies'] == []

    def test_save_requires_both_formulas(self, app, bind_mcp_user):
        with app.app_context():
            with pytest.raises(ToolError):
                self._tool({'action': 'save', 'name': 'x', 'entry_formula': 'A > B'})

    def test_unknown_action_is_rejected(self, app, bind_mcp_user):
        with app.app_context():
            with pytest.raises(ToolError):
                self._tool({'action': 'obliterate'})

    def test_list_explains_the_params_object(self, app, bind_mcp_user):
        """The stored-params field replaced the 'we cannot store this' warning with the
        thing itself — but a null params still has to be called out, or an
        agent re-runs an old strategy at defaults and says nothing."""
        with app.app_context():
            out = self._tool({'action': 'list'})
        assert 'params' in out['note']
        assert 'null' in out['note']

    def test_tool_is_a_write_tool(self):
        """save/delete must be unreachable from a read-only API key."""
        from carrac.mcp.protocol import WRITE_TOOLS
        assert _STRATEGY_TOOL in WRITE_TOOLS

    @pytest.mark.parametrize('tier', ['member', 'tester', 'premium', 'admin'])
    def test_paid_tiers_entitled(self, tier):
        reg = TOOL_REGISTRY[_STRATEGY_TOOL]
        assert tool_entitled(tier, reg['min_tier'], reg['feature'])

    def test_free_tier_blocked(self):
        reg = TOOL_REGISTRY[_STRATEGY_TOOL]
        assert not tool_entitled('free', reg['min_tier'], reg['feature'])


class TestFullSimulationPersistence:
    """Full Simulation had no save path at all."""

    def test_model_layer_accepts_full_simulation(self, app):
        from carrac.backtests.models import save_run, get_run
        from carrac.mcp.backtest_models import MODEL_FULL_SIMULATION
        with app.app_context():
            run_id = save_run(
                'sim-user', symbol='BTC', expression='RSI(14) < 30',
                timeframe='1D', lookback_bars=500, hold_bars=None,
                result={'winRate': 0.58, 'totalReturnPercent': 12.0, 'trades': 9},
                name='full sim', model=MODEL_FULL_SIMULATION,
                config={'entry_expression': 'RSI(14) < 30', 'stop_percent': 2},
            )
            run = get_run('sim-user', run_id)
        assert run['model'] == MODEL_FULL_SIMULATION
        assert run['hold_bars'] is None
        assert run['config']['stop_percent'] == 2

    def test_list_reports_each_models_own_headline_numbers(self, app):
        """A Hold-N hit rate and a Full-Sim win rate must not share a column."""
        from carrac.backtests.models import save_run, list_runs
        from carrac.mcp.backtest_models import MODEL_FULL_SIMULATION
        with app.app_context():
            save_run('mixed-user', symbol='BTC', expression='A', timeframe='1D',
                     lookback_bars=365, hold_bars=5,
                     result={'hit_rate': 0.66, 'mean_return': 0.04, 'trades': 3},
                     model=MODEL_HOLD_N)
            save_run('mixed-user', symbol='BTC', expression='A', timeframe='1D',
                     lookback_bars=365, result={'winRate': 0.58, 'trades': 9},
                     model=MODEL_FULL_SIMULATION)
            runs = list_runs('mixed-user')

        by_model = {r['model']: r for r in runs}
        assert by_model[MODEL_HOLD_N]['hit_rate'] == 0.66
        assert 'winRate' not in by_model[MODEL_HOLD_N]
        assert by_model[MODEL_FULL_SIMULATION]['winRate'] == 0.58
        assert 'hit_rate' not in by_model[MODEL_FULL_SIMULATION]

    def test_historical_analog_cannot_be_persisted_as_a_run(self, app):
        """It's a query against the Quant DB, not a run anyone owns."""
        from carrac.backtests.models import save_run
        from carrac.mcp.backtest_models import MODEL_HISTORICAL_ANALOG
        with app.app_context():
            with pytest.raises(ValueError):
                save_run('u', symbol='BTC', expression='A', timeframe='1D',
                         lookback_bars=1, result={}, model=MODEL_HISTORICAL_ANALOG)

    def test_simulate_exposes_save_as(self):
        schema = TOOL_REGISTRY['backtest_simulate']['inputSchema']
        assert 'save_as' in schema['properties']


# ── Assumptions are declared, reachable, and reported ─────────


class TestModellingAssumptionsSurfaced:
    """Three choices materially move a backtest result. They must not be
    hardcoded and invisible; a result that flatters itself silently is the
    exact thing this product claims not to do."""

    @staticmethod
    def _schema():
        return TOOL_REGISTRY['backtest_simulate']['inputSchema']['properties']

    @pytest.mark.parametrize('param', [
        'gap_aware_fills', 'same_bar_precedence', 'entry_mode',
    ])
    def test_assumption_is_a_named_parameter(self, param):
        assert param in self._schema()

    @pytest.mark.parametrize('param,expected', [
        ('same_bar_precedence', {'stop_first', 'target_first'}),
        ('entry_mode', {'next_open', 'close'}),
    ])
    def test_enumerated_so_a_typo_cannot_pass(self, param, expected):
        assert set(self._schema()[param]['enum']) == expected

    def test_descriptions_name_the_direction_of_bias(self):
        """A flag nobody can interpret is no better than a hidden one."""
        schema = self._schema()
        assert 'look-ahead' in schema['entry_mode']['description'].lower()
        assert 'optimistic' in schema['same_bar_precedence']['description'].lower()

    def test_tool_description_points_at_the_assumptions_block(self):
        desc = TOOL_REGISTRY['backtest_simulate']['description']
        assert 'assumptions' in desc
        assert 'same_bar_precedence' in desc

    # ── the reported block ────────────────────────────────────────────

    @staticmethod
    def _block(**kw):
        from carrac.mcp.tools.backtest import _assumptions_block
        args = {'gap_aware_fills': True, 'same_bar_precedence': 'stop_first',
                'entry_mode': 'next_open', 'stop_type': 'intrabar'}
        args.update(kw)
        return _assumptions_block(args['gap_aware_fills'], args['same_bar_precedence'],
                                  args['entry_mode'], args['stop_type'])

    def test_defaults_are_reported_as_defaults(self):
        b = self._block()
        assert b['entry_fill']['default'] is True
        assert b['same_bar_stop_and_target']['default'] is True
        assert b['gap_aware_fills']['default'] is True

    def test_look_ahead_entry_is_flagged_optimistic(self):
        b = self._block(entry_mode='close')
        assert b['entry_fill']['default'] is False
        assert 'OPTIMISTIC' in b['entry_fill']['bias']
        assert 'look-ahead' in b['entry_fill']['bias'].lower()

    def test_target_first_is_flagged_optimistic(self):
        b = self._block(same_bar_precedence='target_first')
        assert 'OPTIMISTIC' in b['same_bar_stop_and_target']['bias']

    def test_nominal_fills_are_flagged_optimistic(self):
        b = self._block(gap_aware_fills=False)
        assert 'OPTIMISTIC' in b['gap_aware_fills']['bias']

    def test_stop_dependent_assumptions_marked_inapplicable_without_a_stop(self):
        b = self._block(stop_type='close')
        assert b['same_bar_stop_and_target']['applies'] is False
        assert b['gap_aware_fills']['applies'] is False
        assert 'applies' not in b['entry_fill']  # entry fill always applies


class TestUiEntryFillDefault:
    """The look-ahead default that shipped for months.

    backtester.js defaults entryMode to next_open specifically to avoid
    look-ahead bias, commenting that 'close' must be explicitly opted into.
    The UI listed 'close' FIRST (so the browser selected it) and passed it
    explicitly, so every in-app backtest ran look-ahead and diverged from the
    same test over MCP.
    """

    @staticmethod
    def _ui_source():
        from pathlib import Path
        root = Path(__file__).resolve().parent.parent
        return (root / 'static' / 'js' / 'backtest-ui.js').read_text(encoding='utf-8')

    def test_next_open_is_the_first_option(self):
        """First <option> is what the browser selects with no explicit
        `selected` attribute — that is the real default."""
        import re
        src = self._ui_source()
        sel = re.search(r"id=\"bt-entry-mode\".*?</select>", src, re.S)
        assert sel, 'bt-entry-mode select not found — did the panel move?'
        options = re.findall(r"<option value=\"(\w+)\"", sel.group(0))
        assert options[0] == 'next_open', (
            f'entry-fill options are {options}; next_open must be first or the '
            f'browser silently selects the look-ahead reading'
        )

    def test_read_fallback_is_next_open(self):
        assert "_val('bt-entry-mode', 'next_open')" in self._ui_source()
        assert "_val('bt-entry-mode', 'close')" not in self._ui_source()

    def test_engine_default_is_next_open(self):
        """The UI must not contradict the engine it drives."""
        from pathlib import Path
        root = Path(__file__).resolve().parent.parent
        js = (root / 'static' / 'js' / 'backtester.js').read_text(encoding='utf-8')
        assert "config.entryMode || 'next_open'" in js


class TestAssumptionsReachTheCacheKey:
    """Every assumption must be part of the cache key.

    Caught during implementation: the three new parameters were accepted,
    validated, and passed to the engine, but the 6h result cache keyed only on
    the original parameters. So asking for a different assumption returned the
    cached default — accepted, ignored, and successful. That is precisely the
    silent-substitution class the flags exist to eliminate.
    """

    @staticmethod
    def _key(**kw):
        from carrac.mcp.tools.backtest import _sim_cache_key
        base = {'symbol': 'BTC', 'entry': 'a', 'exit': 'b', 'timeframe': '1D',
                'lookback_bars': 400, 'direction': 'long',
                'gap_aware_fills': True, 'same_bar_precedence': 'stop_first',
                'entry_mode': 'next_open'}
        base.update(kw)
        return _sim_cache_key(**base)

    def test_baseline_is_stable(self):
        assert self._key() == self._key()

    @pytest.mark.parametrize('param,other', [
        ('gap_aware_fills', False),
        ('same_bar_precedence', 'target_first'),
        ('entry_mode', 'close'),
    ])
    def test_changing_an_assumption_changes_the_key(self, param, other):
        assert self._key() != self._key(**{param: other}), (
            f'{param} is missing from the cache key — a caller changing it '
            f'would silently receive the cached default result'
        )

    def test_call_site_passes_all_three(self):
        """The key function takes **kwargs, so a forgotten argument is silent."""
        import inspect
        from carrac.mcp.tools import backtest as bt
        src = inspect.getsource(bt.backtest_simulate)
        head = src[:src.index('cache_key =') + src[src.index('cache_key ='):].index(')')]
        for param in ('gap_aware_fills', 'same_bar_precedence', 'entry_mode'):
            assert f'{param}=' in head, f'{param} not passed to _sim_cache_key'


class TestStrategyParamsRoundTrip:
    """Save must not drop any of the panel's parameters, which the
    `strategies` table has no columns for. One `params` JSON column
    carries the whole setup; the seven legacy scalars are still written."""

    FULL_SETUP = {
        'timeframe': '4h', 'start_date': '2024-01-01', 'end_date': '2024-12-31',
        'regime': 'favorable', 'instrument': 'perp', 'slippage_bps': 5,
        'stop_type': 'intrabar', 'stop_loss_percent': 2, 'take_profit_percent': 4,
        'entry_mode': 'next_open',
    }

    def test_browser_save_load_preserves_every_field(self, app, auth_client):
        body = {'name': 'full setup', 'entry_formula': 'A > B', 'exit_formula': 'A < B'}
        body.update(self.FULL_SETUP)
        assert auth_client.post('/api/strategies', json=body).status_code == 201

        got = auth_client.get('/api/strategies').get_json()['strategies'][0]
        assert got['params'] == self.FULL_SETUP, (
            'the whole setup must survive the round trip'
        )

    def test_agent_save_load_preserves_every_field(self, app, auth_client, bind_mcp_user):
        bind_mcp_user(uid='test-user')
        args = {'action': 'save', 'name': 'agent full', 'entry_formula': 'A > B',
                'exit_formula': 'A < B'}
        args.update(self.FULL_SETUP)
        with app.app_context():
            TOOL_REGISTRY[_STRATEGY_TOOL]['handler'](args)
            listed = TOOL_REGISTRY[_STRATEGY_TOOL]['handler']({'action': 'list'})
        assert listed['strategies'][0]['params'] == self.FULL_SETUP

    def test_cross_surface_params_survive(self, app, auth_client, bind_mcp_user):
        """Browser saves the full setup; the agent sees all of it."""
        bind_mcp_user(uid='test-user')
        body = {'name': 'x', 'entry_formula': 'A > B', 'exit_formula': 'A < B'}
        body.update(self.FULL_SETUP)
        auth_client.post('/api/strategies', json=body)
        with app.app_context():
            out = TOOL_REGISTRY[_STRATEGY_TOOL]['handler']({'action': 'list'})
        assert out['strategies'][0]['params']['stop_loss_percent'] == 2
        assert out['strategies'][0]['params']['entry_mode'] == 'next_open'

    def test_absent_params_stay_null_not_zero_filled(self, app, auth_client):
        """A strategy saved without a stop must not gain a 0% one."""
        auth_client.post('/api/strategies', json={
            'name': 'bare', 'entry_formula': 'A > B', 'exit_formula': 'A < B'})
        got = auth_client.get('/api/strategies').get_json()['strategies'][0]
        assert got['params'] is None

    def test_unknown_keys_are_not_stored(self, app, auth_client):
        """The blob is whitelisted, not a passthrough."""
        auth_client.post('/api/strategies', json={
            'name': 'y', 'entry_formula': 'A > B', 'exit_formula': 'A < B',
            'timeframe': '1D', 'evil': 'x' * 10000})
        got = auth_client.get('/api/strategies').get_json()['strategies'][0]
        assert got['params'] == {'timeframe': '1D'}

    def test_legacy_scalars_still_written(self, app, auth_client):
        """Old readers must keep working."""
        auth_client.post('/api/strategies', json={
            'name': 'z', 'entry_formula': 'A > B', 'exit_formula': 'A < B',
            'direction': 'short', 'capital': 5000, 'commission_percent': 0.25,
            'timeframe': '1W'})
        got = auth_client.get('/api/strategies').get_json()['strategies'][0]
        assert got['direction'] == 'short'
        assert got['capital'] == 5000
        assert got['commission_percent'] == 0.25

    def test_ui_sends_every_dropped_field(self):
        """Source guard — the POST body must carry all nine, plus timeframe."""
        from pathlib import Path
        src = (Path(__file__).resolve().parent.parent
               / 'static' / 'js' / 'backtest-ui.js').read_text(encoding='utf-8')
        body = src[src.index('var body = {'):src.index('fetch(\'/api/strategies\'')]
        for field in ('timeframe', 'start_date', 'end_date', 'regime', 'instrument',
                      'slippage_bps', 'stop_type', 'stop_loss_percent',
                      'take_profit_percent', 'entry_mode'):
            assert f'{field}:' in body, f'saveStrategy() no longer sends {field}'


class TestSaveRunPersistsWhatWasReviewed:
    """backtest_save_run re-ran the backtest instead of storing
    the result the caller had already shown the user."""

    REVIEWED = {'trades': 7, 'hit_rate': 0.99, 'mean_return': 0.5}

    def test_supplied_result_is_stored_verbatim(self, app, bind_mcp_user, monkeypatch):
        # A re-run would return something completely different.
        monkeypatch.setattr('carrac.mcp.tools.backtests._run_backtest',
                            lambda _a: {'trades': 1, 'hit_rate': 0.01})
        with app.app_context():
            out = SAVE_FN()({'symbol': 'BTC', 'expression': 'RSI(14) < 30',
                             'result': dict(self.REVIEWED)})
            fetched = GET_FN()({'run_id': out['run_id']})
        assert out['result_source'] == 'caller'
        assert fetched['result']['hit_rate'] == 0.99, 'stored the re-run, not the review'

    def test_omitted_result_recomputes_and_says_so(self, app, bind_mcp_user, monkeypatch):
        _mock_backtest(monkeypatch)
        with app.app_context():
            out = SAVE_FN()({'symbol': 'BTC', 'expression': 'RSI(14) < 30'})
        assert out['result_source'] == 'recomputed'
        assert 're-run' in out['result_source_note']

    def test_provenance_is_stamped_on_the_row(self, app, bind_mcp_user, monkeypatch):
        """Not just in the response — a saved run must answer this later."""
        _mock_backtest(monkeypatch)
        with app.app_context():
            out = SAVE_FN()({'symbol': 'BTC', 'expression': 'RSI(14) < 30',
                             'result': dict(self.REVIEWED)})
            fetched = GET_FN()({'run_id': out['run_id']})
        assert fetched['config']['result_source'] == 'caller'

    def test_failed_result_is_refused(self, app, bind_mcp_user):
        with app.app_context():
            with pytest.raises(ToolError, match='Refusing to save'):
                SAVE_FN()({'symbol': 'BTC', 'expression': 'RSI(14) < 30',
                           'result': {'error': 'no data'}})

    def test_non_object_result_is_refused(self, app, bind_mcp_user):
        with app.app_context():
            with pytest.raises(ToolError):
                SAVE_FN()({'symbol': 'BTC', 'expression': 'RSI(14) < 30',
                           'result': 'looks fine to me'})

    def test_result_is_a_declared_parameter(self):
        assert 'result' in TOOL_REGISTRY['backtest_save_run']['inputSchema']['properties']


# ── HTTP route: POST /api/backtest/runs (Task A — browser saves a RUN) ───────
#
# The browser's Run Backtest button computes the Full Simulation model
# entirely client-side (static/js/backtester.js -> window.Backtester.run()).
# This route only stores the result the browser already computed and
# displayed — it must never invoke the server-side simulation engine
# (carrac.backtest_sim.run_backtest): it stores what was reviewed, as on the
# MCP door (TestSaveRunPersistsWhatWasReviewed above).

_FULL_SIM_RESULT = {
    'totalTrades': 4,
    'winRate': 0.75,
    'profitFactor': 2.1,
    'totalReturnPercent': 12.5,
    'maxDrawdownPercent': 3.2,
    'sharpeRatio': 1.4,
}


def _full_sim_body(**overrides):
    body = {
        'symbol': 'BTC',
        'timeframe': '1D',
        'expression': 'CROSSOVER(EMA(9), EMA(21))',
        'name': 'browser run',
        'lookback_bars': 300,
        'result': dict(_FULL_SIM_RESULT),
        'config': {
            'exit': 'CROSSUNDER(EMA(9), EMA(21))',
            'direction': 'long',
            'instrument': 'spot',
            'initialCapital': 10000,
            'sizePercent': 100,
            'commissionPercent': 0.1,
            'slippageBps': 5,
            'stopType': 'close',
            'stopLossPercent': 2,
            'takeProfitPercent': 4,
            'entryMode': 'next_open',
        },
    }
    body.update(overrides)
    return body


class TestSaveBacktestRunHTTPRoute:
    """POST /api/backtest/runs — the browser's door onto a saved RUN."""

    def test_saves_and_returns_run_id(self, auth_client):
        resp = auth_client.post('/api/backtest/runs', json=_full_sim_body())
        assert resp.status_code == 201
        data = resp.get_json()
        assert data['status'] == 'created'
        assert isinstance(data['run_id'], int)

    def test_stores_full_simulation_model_and_config_vocabulary(self, auth_client):
        run_id = auth_client.post('/api/backtest/runs', json=_full_sim_body()).get_json()['run_id']
        detail = auth_client.get(f'/api/backtest/runs/{run_id}').get_json()
        assert detail['model'] == MODEL_FULL_SIMULATION
        # Same field names carrac/mcp/tools/backtest.py _persist_simulation uses,
        # so a run saved here and one saved via MCP are indistinguishable.
        cfg = detail['config']
        assert cfg['entry_expression'] == 'CROSSOVER(EMA(9), EMA(21))'
        assert cfg['exit_expression'] == 'CROSSUNDER(EMA(9), EMA(21))'
        assert cfg['stop_percent'] == 2
        assert cfg['target_percent'] == 4
        assert cfg['size_percent'] == 100
        assert cfg['commission_percent'] == 0.1
        assert cfg['slippage_bps'] == 5
        assert cfg['initial_capital'] == 10000
        assert cfg['entry_mode'] == 'next_open'

    def test_free_tier_is_blocked(self, client):
        with client.session_transaction() as sess:
            sess['user_id'] = 'free-user'
            sess['email'] = 'free@example.com'
            sess['tier'] = 'free'
        resp = client.post('/api/backtest/runs', json=_full_sim_body())
        assert resp.status_code == 403
        assert resp.get_json()['upgrade_required'] is True

    def test_rejects_model_outside_persistable_set(self, auth_client):
        resp = auth_client.post('/api/backtest/runs',
                                json=_full_sim_body(model='Historical Analog'))
        assert resp.status_code == 400
        assert 'error' in resp.get_json()

    def test_rejects_missing_result(self, auth_client):
        body = _full_sim_body()
        del body['result']
        resp = auth_client.post('/api/backtest/runs', json=body)
        assert resp.status_code == 400

    def test_rejects_missing_symbol(self, auth_client):
        resp = auth_client.post('/api/backtest/runs', json=_full_sim_body(symbol=''))
        assert resp.status_code == 400

    def test_login_required(self, client):
        resp = client.post('/api/backtest/runs', json=_full_sim_body())
        assert resp.status_code in (302, 401)


class TestSaveBacktestRunDoesNotRecompute:
    """Guard, HTTP door: this route must store what was submitted,
    not repeat the simulation."""

    def test_engine_is_never_invoked(self, auth_client, monkeypatch):
        """The Python Full-Sim engine must not even be called — the browser
        already ran the simulation client-side before this request was sent."""
        def _boom(*a, **k):
            raise AssertionError('the route recomputed instead of storing')
        monkeypatch.setattr('carrac.backtest_sim.run_backtest', _boom)

        resp = auth_client.post('/api/backtest/runs', json=_full_sim_body())
        assert resp.status_code == 201

    def test_stored_numbers_are_exactly_what_was_submitted(self, auth_client):
        """A recompute would not reproduce these exact, made-up figures —
        round-tripping them unchanged proves nothing was recomputed."""
        reviewed = {
            'totalTrades': 13,
            'winRate': 0.4231,
            'totalReturnPercent': -7.77,
            'maxDrawdownPercent': 41.9,
            'sharpeRatio': -0.6,
        }
        body = _full_sim_body(result=dict(reviewed))
        run_id = auth_client.post('/api/backtest/runs', json=body).get_json()['run_id']
        detail = auth_client.get(f'/api/backtest/runs/{run_id}').get_json()
        for k, v in reviewed.items():
            assert detail['result'][k] == v
        assert detail['result']['trades'] == reviewed['totalTrades']  # summary alias


class TestBrowserSavedRunVisibleToMCP:
    """A run saved through the browser POST route must appear over MCP,
    completing the both-doors guarantee in the write direction —
    TestBacktestRunsVisibleInBothDoors above already covers MCP -> web."""

    def test_browser_saved_run_is_visible_via_backtest_get_runs(self, app, auth_client,
                                                                 bind_mcp_user):
        bind_mcp_user(uid='test-user')  # same identity auth_client logs in as
        resp = auth_client.post('/api/backtest/runs',
                                json=_full_sim_body(name='from the browser'))
        assert resp.status_code == 201
        run_id = resp.get_json()['run_id']

        with app.app_context():
            listed = TOOL_REGISTRY['backtest_get_runs']['handler']({})
            got = TOOL_REGISTRY['backtest_get_runs']['handler']({'run_id': run_id})

        assert run_id in [r['run_id'] for r in listed['runs']]
        assert MODEL_FULL_SIMULATION in listed['models']
        assert got['name'] == 'from the browser'
        assert got['result']['winRate'] == _FULL_SIM_RESULT['winRate']
