"""DSL variable symbol-resolution semantics.

quant_vars and derivatives_vars share syntax, so both must resolve per symbol:
`FUNDING < 0` on an ETH chart uses ETH funding, never a global (BTC) value.

The contract:
  - FUNDING and DVOL are per-symbol (they are quant.derivatives columns —
    funding_rate_avg has rows for 10 assets, dvol for BTC/ETH).
  - A per-symbol var with no data for the formula's symbol fails LOUDLY with
    a message naming the var and the symbol — never "Unknown identifier",
    never a silent substitution.
  - scanner/consensio/ribbon/hurst are REMOVED: nothing
    writes them into quant.signals (no source='convexity' rows), so the vars
    never resolved.
  - Every advertised var carries a machine-readable scope in
    indicator_get_dsl_reference: global | per_symbol | btc_reference.
  - The remaining BTC-reference var (etf_flow) warns when used on a
    non-BTC symbol instead of silently substituting.
  - The JS parser mirror (static/js/formula-parser.js) carries the SAME
    tables — parsed from the real file, not a hardcoded copy.
"""
import re
from pathlib import Path

import numpy as np

from carrac.formula_eval import (
    DERIV_VARS, QUANT_VARS, VAR_SCOPE,
    evaluate_formula, get_deriv_deps, get_quant_deps,
)

ROOT = Path(__file__).resolve().parents[1]


# ── Table-level contract ─────────────────────────────────────────────

class TestVarTables:
    def test_funding_and_dvol_are_per_symbol(self):
        assert 'funding' in DERIV_VARS and DERIV_VARS['funding'] == 'funding_rate_avg'
        assert 'dvol' in DERIV_VARS and DERIV_VARS['dvol'] == 'dvol'
        assert 'funding' not in QUANT_VARS
        assert 'dvol' not in QUANT_VARS

    def test_dead_signal_vars_removed(self):
        # store_signals has no live caller and quant.signals holds zero
        # source='convexity' rows — these vars never resolved to data.
        for dead in ('scanner', 'consensio', 'ribbon', 'hurst'):
            assert dead not in QUANT_VARS, f'{dead} is a dead var — remove it'

    def test_every_var_has_a_scope(self):
        for name in list(QUANT_VARS) + list(DERIV_VARS):
            assert name in VAR_SCOPE, f'{name} has no declared scope'
            assert VAR_SCOPE[name] in ('global', 'per_symbol', 'btc_reference')

    def test_scope_assignments(self):
        assert VAR_SCOPE['funding'] == 'per_symbol'
        assert VAR_SCOPE['dvol'] == 'per_symbol'
        assert VAR_SCOPE['iv_30d'] == 'per_symbol'
        assert VAR_SCOPE['etf_flow'] == 'btc_reference'
        assert VAR_SCOPE['vix'] == 'global'
        assert VAR_SCOPE['regime'] == 'global'


# ── Dependency scanning follows the move ─────────────────────────────

class TestDepScan:
    def test_funding_scans_as_deriv_dep(self):
        assert get_deriv_deps('FUNDING < 0') == [('funding', 'funding_rate_avg')]
        assert get_quant_deps('FUNDING < 0') == []

    def test_macro_vars_still_scan_as_quant_deps(self):
        assert get_quant_deps('VIX > 20') == [('vix', 'VIX')]
        assert get_deriv_deps('VIX > 20') == []


# ── Loud failure instead of silent substitution ──────────────────────

def _stub_ohlcv(n=60):
    dates = np.arange(np.datetime64('2026-06-01'), np.datetime64('2026-06-01') + np.timedelta64(n, 'D'))
    arr = np.linspace(100.0, 110.0, n)
    return {
        'open': arr, 'high': arr + 1, 'low': arr - 1, 'close': arr,
        'volume': np.full(n, 5.0),
        '_dates': dates.astype('datetime64[ns]'),
        '_total_available': n,
    }


class TestMissingPerSymbolData:
    def test_no_data_error_names_var_and_symbol(self, monkeypatch):
        import carrac.formula_eval as fe
        monkeypatch.setattr(fe, '_load_ohlcv_cached',
                            lambda *a, **k: _stub_ohlcv())
        monkeypatch.setattr(fe, '_load_deriv_series_cached',
                            lambda *a, **k: None)
        out = evaluate_formula('FUNDING < 0', 'GOLD', '1D')
        assert out['error'], 'missing per-symbol data must be an error'
        assert 'FUNDING' in out['error'] and 'GOLD' in out['error'], out['error']
        assert 'Unknown identifier' not in out['error']


# ── BTC-reference warning ────────────────────────────────────────────

class TestBtcReferenceWarning:
    def _eval(self, monkeypatch, symbol):
        import carrac.formula_eval as fe
        from datetime import date, timedelta
        monkeypatch.setattr(fe, '_load_ohlcv_cached',
                            lambda *a, **k: _stub_ohlcv())
        start = date(2026, 6, 1)
        series = [(start + timedelta(days=i), 100.0) for i in range(60)]
        monkeypatch.setattr(fe, '_load_quant_series_cached',
                            lambda *a, **k: series)
        return evaluate_formula('ETF_FLOW > 0', symbol, '1D')

    def test_non_btc_symbol_gets_warning(self, monkeypatch):
        out = self._eval(monkeypatch, 'ETH')
        assert out['error'] is None
        warns = out.get('warnings') or []
        assert any('ETF_FLOW' in w and 'BTC' in w for w in warns), \
            f'expected a BTC-reference warning, got {warns}'

    def test_btc_symbol_gets_no_warning(self, monkeypatch):
        out = self._eval(monkeypatch, 'BTC')
        assert not out.get('warnings')


# ── JS parser mirror stays in sync (parsed, not hardcoded) ───────────

def _js_object_keys(block_name):
    src = (ROOT / 'static/js/formula-parser.js').read_text(encoding='utf-8')
    m = re.search(block_name + r':\s*\{(.*?)\}', src, re.DOTALL)
    assert m, f'{block_name} object not found in formula-parser.js'
    body = '\n'.join(line.split('//')[0] for line in m.group(1).splitlines())
    return set(re.findall(r'\b([a-z_][a-z0-9_]*)\s*:', body))


class TestJsMirror:
    def test_quant_vars_match_python(self):
        assert _js_object_keys('QUANT_VARS') == set(QUANT_VARS)

    def test_deriv_vars_match_python(self):
        assert _js_object_keys('DERIV_VARS') == set(DERIV_VARS)


# ── The DSL reference documents scope ─────────────────────────

class TestDslReferenceScopes:
    def test_reference_exposes_var_scopes(self, bind_mcp_user):
        from carrac.mcp import tools  # noqa: F401
        from carrac.mcp.registry import TOOL_REGISTRY
        out = TOOL_REGISTRY['indicator_get_dsl_reference']['handler']({})
        assert 'var_scopes' in out
        for name in out['quant_vars'] + out['derivatives_vars']:
            assert name in out['var_scopes'], f'{name} missing from var_scopes'
        assert out['var_scopes']['funding'] == 'per_symbol'
        assert out['var_scopes']['etf_flow'] == 'btc_reference'
