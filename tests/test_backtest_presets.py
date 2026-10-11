"""Every shipped preset must parse and use only functions present in BOTH
engines. A preset referencing a one-engine function is a user-facing break:
it would work in the drawer and fail via MCP, or vice versa.

"""

import json
import re
from pathlib import Path

import pytest

from carrac.formula_eval import validate_formula
from tests.test_formula_registry_parity import (
    _js_registry_names, _py_registry_names,
)

ROOT = Path(__file__).resolve().parent.parent
BT_UI = ROOT / 'static' / 'js' / 'backtest-ui.js'


def _presets():
    """Extract the BT_PATTERN_PRESETS literal from backtest-ui.js."""
    src = BT_UI.read_text()
    m = re.search(r'BT_PATTERN_PRESETS\s*=\s*(\[.*?\]);', src, re.S)
    assert m, 'BT_PATTERN_PRESETS not found in backtest-ui.js'
    # The literal is JSON-compatible by construction (double-quoted keys).
    return json.loads(m.group(1))


def test_presets_exist():
    assert len(_presets()) >= 4


@pytest.mark.parametrize('preset', _presets(), ids=lambda p: p['label'])
def test_preset_formula_parses(preset):
    result = validate_formula(preset['formula'])
    assert result['valid'], f"{preset['label']}: {result['error']}"


@pytest.mark.parametrize('preset', _presets(), ids=lambda p: p['label'])
def test_preset_uses_only_dual_engine_functions(preset):
    both = _py_registry_names() & _js_registry_names()
    called = set(re.findall(r'([a-zA-Z_][a-zA-Z0-9_]*)\s*\(', preset['formula']))
    unknown = {f.lower() for f in called} - both
    assert not unknown, (
        f"{preset['label']} calls {sorted(unknown)}, which is not in both "
        'engines — it would work in one surface and fail in the other.'
    )


@pytest.mark.parametrize('preset', _presets(), ids=lambda p: p['label'])
def test_preset_label_does_not_claim_to_be_an_order_block(preset):
    """Carrac draws structure-anchored order-block zones with mitigation
    (smart-money-concepts.js). No preset here computes that, so none may
    borrow the name — that is what two disagreeing definitions of
    one SMC term cost."""
    label = preset['label'].lower()
    assert 'order block' not in label and 'order-block' not in label, (
        f"Preset {preset['label']!r} claims to be an order block but is a "
        'candle-sequence formula. Rename it or drop it.'
    )
