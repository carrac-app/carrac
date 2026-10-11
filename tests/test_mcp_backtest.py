"""Tests for backtest_play and scan_and_backtest."""

import pytest

from carrac.mcp import tools  # noqa: F401  registration
from carrac.mcp.registry import TOOL_REGISTRY
from carrac.mcp.protocol import ToolError
from carrac.mcp.tools.backtest import (
    synthesize_play_formula, backtest_play, scan_and_backtest,
)


class TestFormulaSynthesis:
    def test_indicator_value_synth(self):
        conds = {
            'logic': 'all',
            'rules': [
                {'type': 'indicator_value',
                 'params': {'indicator': 'RSI', 'operator': 'lt', 'value': 30}},
            ],
        }
        expr, used, skipped = synthesize_play_formula(conds)
        assert 'RSI(14) < 30' in expr
        assert used == 1
        assert skipped == []

    def test_price_level_synth(self):
        conds = {
            'logic': 'all',
            'rules': [
                {'type': 'price_level', 'params': {'level': 50000, 'relation': 'above'}},
            ],
        }
        expr, used, _ = synthesize_play_formula(conds)
        assert '(close > 50000)' in expr

    def test_formula_expression_passthrough(self):
        conds = {'logic': 'all', 'rules': [
            {'type': 'formula_expression', 'params': {'expression': 'RSI(14) > 70'}},
        ]}
        expr, _, _ = synthesize_play_formula(conds)
        assert 'RSI(14) > 70' in expr

    def test_indicator_crosses_to_crossover(self):
        conds = {'logic': 'all', 'rules': [
            {'type': 'indicator_crosses',
             'params': {'indicator': 'RSI', 'direction': 'above', 'value': 50}},
        ]}
        expr, _, _ = synthesize_play_formula(conds)
        assert 'CROSSOVER' in expr

    def test_indicator_crosses_below_to_crossunder(self):
        conds = {'logic': 'all', 'rules': [
            {'type': 'indicator_crosses',
             'params': {'indicator': 'RSI', 'direction': 'below', 'value': 50}},
        ]}
        expr, _, _ = synthesize_play_formula(conds)
        assert 'CROSSUNDER' in expr

    def test_logic_all_uses_AND(self):
        conds = {'logic': 'all', 'rules': [
            {'type': 'price_level', 'params': {'level': 50000, 'relation': 'above'}},
            {'type': 'indicator_value', 'params': {'indicator': 'RSI', 'operator': 'lt', 'value': 30}},
        ]}
        expr, used, _ = synthesize_play_formula(conds)
        assert ' AND ' in expr
        assert used == 2

    def test_logic_any_uses_OR(self):
        conds = {'logic': 'any', 'rules': [
            {'type': 'price_level', 'params': {'level': 50000, 'relation': 'above'}},
            {'type': 'price_level', 'params': {'level': 60000, 'relation': 'below'}},
        ]}
        expr, _, _ = synthesize_play_formula(conds)
        assert ' OR ' in expr

    def test_unsupported_types_reported(self):
        conds = {'logic': 'all', 'rules': [
            {'type': 'manual', 'params': {'label': 'check news'}},
            {'type': 'trendline_break', 'params': {'drawing_id': 1, 'direction': 'above'}},
            {'type': 'price_level', 'params': {'level': 50000, 'relation': 'above'}},
        ]}
        expr, used, skipped = synthesize_play_formula(conds)
        assert used == 1
        assert 'manual' in skipped
        assert 'trendline_break' in skipped

    def test_empty_returns_none(self):
        expr, used, _ = synthesize_play_formula({'logic': 'all', 'rules': []})
        assert expr is None
        assert used == 0

    def test_invalid_input_returns_none(self):
        expr, used, _ = synthesize_play_formula(None)
        assert expr is None

    def test_invalid_params_skipped(self):
        conds = {'logic': 'all', 'rules': [
            {'type': 'indicator_value', 'params': {'indicator': 'RSI'}},  # missing op + value
        ]}
        expr, used, skipped = synthesize_play_formula(conds)
        assert used == 0
        assert any('indicator_value' in s for s in skipped)


class TestBacktestPlayTool:
    """backtest_play is a plain helper now, absorbed into
    backtest_run (source=play) — no longer a standalone registry entry."""

    def test_missing_play_id_raises(self):
        with pytest.raises(ToolError):
            backtest_play({})

    def test_unknown_play_raises(self, app):
        with app.app_context():
            with pytest.raises(ToolError):
                backtest_play({'play_id': 9999999})


class TestScanAndBacktestTool:
    """scan_and_backtest is a plain helper now, absorbed into
    backtest_run (source=scan) — no longer a standalone registry entry."""

    def test_missing_expression_raises(self):
        with pytest.raises(ToolError):
            scan_and_backtest({})

    def test_max_assets_clamped(self, app, monkeypatch):
        # Force scanner to return 100 fake symbols, then ensure clamp.
        from carrac.mcp.tools import scanner as scanner_mod

        fake_results = [{'symbol': f'COIN{i}'} for i in range(100)]
        monkeypatch.setattr(
            scanner_mod, 'scanner_get_screen', lambda args: {'results': fake_results}
        )
        # Patch backtest_indicator_formula to a no-op that just returns dict.
        from carrac.mcp.tools import indicators as ind_mod

        def fake_bt(args):
            return {'symbol': args['symbol'], 'trades': 0, 'hit_rate': 0.5, 'mean_return': 0.01}

        monkeypatch.setattr(ind_mod, 'backtest_indicator_formula', fake_bt)
        # Re-import to pick up patches.
        from carrac.mcp.tools.backtest import scan_and_backtest as fn

        with app.app_context():
            out = fn({'expression': 'RSI(14) < 30', 'max_assets': 50})
        # Should clamp at 25.
        assert out['count'] <= 25


class TestModelField:
    """backtest_play and scan_and_backtest must carry a
    top-level 'model': 'Hold-N Signal Test' — they run the same Hold-N model
    as backtest_indicator_formula, and a caller must never mistake the
    result for Full Simulation or Historical Analog output."""

    def test_backtest_play_inherits_model_from_indicator_formula(
        self, app, bind_mcp_user, monkeypatch,
    ):
        from carrac.plays import models as plays_models
        from carrac.mcp.tools import indicators as ind_mod
        from carrac.mcp.backtest_models import MODEL_HOLD_N

        fake_play = {
            'id': 1, 'title': 'Test play', 'primary_asset': 'BTC', 'timeframe': '1D',
            'conditions': {'logic': 'all', 'rules': [
                {'type': 'price_level', 'params': {'level': 50000, 'relation': 'above'}},
            ]},
        }
        monkeypatch.setattr(plays_models, 'get_play', lambda play_id, user_id=None: fake_play)

        def fake_bt(args):
            return {'model': MODEL_HOLD_N, 'symbol': args['symbol'], 'trades': 3, 'hit_rate': 0.5}

        monkeypatch.setattr(ind_mod, 'backtest_indicator_formula', fake_bt)
        from carrac.mcp.tools.backtest import backtest_play as fn

        with app.app_context():
            out = fn({'play_id': 1})

        assert out['model'] == MODEL_HOLD_N == 'Hold-N Signal Test'
        assert out['play_id'] == 1

    def test_scan_and_backtest_sets_model_unconditionally(self, app, monkeypatch):
        """The top-level 'model' key must be present even if a per-symbol
        result (from a monkeypatched/legacy caller) omits it."""
        from carrac.mcp.tools import scanner as scanner_mod
        from carrac.mcp.tools import indicators as ind_mod
        from carrac.mcp.backtest_models import MODEL_HOLD_N

        monkeypatch.setattr(
            scanner_mod, 'scanner_get_screen', lambda args: {'results': [{'symbol': 'BTC'}]}
        )

        def fake_bt(args):
            # Deliberately omits 'model' to prove scan_and_backtest sets its
            # own top-level key, not merely relying on what results contain.
            return {'symbol': args['symbol'], 'trades': 0, 'hit_rate': 0.5, 'mean_return': 0.01}

        monkeypatch.setattr(ind_mod, 'backtest_indicator_formula', fake_bt)
        from carrac.mcp.tools.backtest import scan_and_backtest as fn

        with app.app_context():
            out = fn({'expression': 'RSI(14) < 30'})

        assert out['model'] == MODEL_HOLD_N

    def test_scan_and_backtest_no_symbols_still_carries_model(self, app, monkeypatch):
        from carrac.mcp.tools import scanner as scanner_mod
        from carrac.mcp.backtest_models import MODEL_HOLD_N

        monkeypatch.setattr(scanner_mod, 'scanner_get_screen', lambda args: {'results': []})
        from carrac.mcp.tools.backtest import scan_and_backtest as fn

        with app.app_context():
            out = fn({'expression': 'RSI(14) < 30'})

        assert out['count'] == 0
        assert out['model'] == MODEL_HOLD_N


class TestRegistration:
    """backtest_play/scan_and_backtest/backtest_indicator_formula
    were consolidated into backtest_run (source=play|scan|formula) — they're
    plain helper functions now, not separate TOOL_REGISTRY entries. The
    legacy names still dispatch via registry.LEGACY_TOOL_ALIASES."""

    def test_backtest_run_registered_not_the_old_names(self):
        assert 'backtest_run' in TOOL_REGISTRY
        assert 'backtest_play' not in TOOL_REGISTRY
        assert 'scan_and_backtest' not in TOOL_REGISTRY
        assert 'backtest_indicator_formula' not in TOOL_REGISTRY

    def test_gated_on_backtester_feature(self):
        """Backtester is a member-and-up TIER_LIMITS knob, not a
        hardcoded premium min_tier."""
        assert TOOL_REGISTRY['backtest_run']['min_tier'] == 'member'
        assert TOOL_REGISTRY['backtest_run']['feature'] == 'backtester'

    def test_legacy_names_still_dispatch_to_backtest_run(self):
        from carrac.mcp.registry import resolve_tool_name
        assert resolve_tool_name('backtest_play') == 'backtest_run'
        assert resolve_tool_name('scan_and_backtest') == 'backtest_run'
        assert resolve_tool_name('backtest_indicator_formula') == 'backtest_run'

    def test_source_inference(self, app, monkeypatch):
        """backtest_run infers `source` when the caller omits it, so legacy
        callers of the three pre-rename tools keep working unchanged."""
        from carrac.mcp.tools.backtest import backtest_run

        calls = []
        monkeypatch.setattr(
            'carrac.mcp.tools.backtest.backtest_play',
            lambda args: calls.append(('play', args)) or {'model': 'x'},
        )
        monkeypatch.setattr(
            'carrac.mcp.tools.backtest.scan_and_backtest',
            lambda args: calls.append(('scan', args)) or {'model': 'x'},
        )
        monkeypatch.setattr(
            'carrac.mcp.tools.indicators.backtest_indicator_formula',
            lambda args: calls.append(('formula', args)) or {'model': 'x'},
        )

        with app.app_context():
            backtest_run({'play_id': 1})
            backtest_run({'expression': 'RSI(14) < 30'})  # no symbol -> scan
            backtest_run({'symbol': 'BTC', 'expression': 'RSI(14) < 30'})

        assert [c[0] for c in calls] == ['play', 'scan', 'formula']
