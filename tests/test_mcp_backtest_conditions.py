"""Tests for the backtest_get_historical_analog MCP tool (Historical Analog model).

This tool's response must carry a top-level
'model': 'Historical Analog' so a caller can never mistake its
median/p25/p75 forward-return output for a Hold-N or Full Simulation result.
"""

import pytest

from carrac.mcp import tools  # noqa: F401  ensures registration
from carrac.mcp.registry import TOOL_REGISTRY
from carrac.mcp.protocol import ToolError
from carrac.mcp.backtest_models import MODEL_HISTORICAL_ANALOG


def _fake_query_conditional(conditions, target='BTC', horizon=30):
    return {
        'instances': 12,
        'forward_return': {'median': 0.03, 'p25': -0.01, 'p75': 0.08},
        'current_match': False,
    }


@pytest.fixture(autouse=True)
def _mock_query_conditional(monkeypatch):
    import quant.engine.conditional as conditional_mod
    monkeypatch.setattr(conditional_mod, 'query_conditional', _fake_query_conditional)


class TestBacktestConditionsModelField:
    def test_missing_conditions_raises(self):
        fn = TOOL_REGISTRY['backtest_get_historical_analog']['handler']
        with pytest.raises(ToolError):
            fn({})

    def test_response_carries_model(self):
        fn = TOOL_REGISTRY['backtest_get_historical_analog']['handler']
        out = fn({'conditions': 'FEAR_GREED:lt:-1.5', 'target': 'BTC', 'horizons': '7,30'})

        assert out['model'] == MODEL_HISTORICAL_ANALOG == 'Historical Analog'
        assert out['conditions'] == ['FEAR_GREED:lt:-1.5']
        assert out['target'] == 'BTC'
        assert '7d' in out['horizons'] and '30d' in out['horizons']
        assert out['horizons']['7d']['instances'] == 12
        assert out['horizons']['7d']['median_return'] == 0.03


class TestRegistration:
    def test_tool_registered(self):
        assert 'backtest_get_historical_analog' in TOOL_REGISTRY

    def test_gated_on_quant_engine_feature(self):
        reg = TOOL_REGISTRY['backtest_get_historical_analog']
        assert reg['min_tier'] == 'member'
        assert reg['feature'] == 'quant_engine'
