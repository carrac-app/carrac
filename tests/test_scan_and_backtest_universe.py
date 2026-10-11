"""Tests for the scan_and_backtest universe parameter."""

import pytest

from carrac.mcp import tools  # noqa: F401  registration
from carrac.mcp.protocol import ToolError
from carrac.mcp.tools.backtest import scan_and_backtest


# ── fixtures ──────────────────────────────────────────────────────

FAKE_SCANNER_RESULTS = [
    {'symbol': 'BTC', 'best_verdict': 'STRONG', 'long': {'total': 90}},
    {'symbol': 'ETH', 'best_verdict': 'GOOD', 'long': {'total': 80}},
    {'symbol': 'SOL', 'best_verdict': 'NEUTRAL', 'long': {'total': 50}},
    {'symbol': 'ADA', 'best_verdict': 'WEAK', 'long': {'total': 20}},
]

FAKE_SCANNER_DATA = {
    'results': FAKE_SCANNER_RESULTS,
    'macro': {},
    'computed_at': '2026-05-11T00:00:00Z',
}


def _make_fake_bt(args):
    """Stub backtest result with deterministic values per symbol."""
    return {
        'symbol': args['symbol'],
        'trades': 10,
        'hit_rate': 0.6,
        'mean_return': 0.02,
    }


# ── universe="all" ────────────────────────────────────────────────

class TestUniverseAll:
    def test_returns_results_for_all_assets(self, app, monkeypatch):
        """universe=all runs backtest across all scanner assets regardless of verdict."""
        import carrac.caches as caches_mod
        import carrac.mcp.tools.indicators as ind_mod

        monkeypatch.setattr(caches_mod, 'get_cached_scanner', lambda: FAKE_SCANNER_DATA)
        monkeypatch.setattr(ind_mod, 'backtest_indicator_formula', _make_fake_bt)

        with app.app_context():
            out = scan_and_backtest({
                'expression': 'RSI(14) < 35',
                'universe': 'all',
                'max_assets': 25,
            })

        assert out['count'] == 4  # all 4 fake assets, no verdict filter
        symbols = [r['symbol'] for r in out['results']]
        assert 'SOL' in symbols  # NEUTRAL — would be excluded by default
        assert 'ADA' in symbols  # WEAK — would be excluded by default

    def test_warning_field_present(self, app, monkeypatch):
        """universe=all includes a warning field in the response."""
        import carrac.caches as caches_mod
        import carrac.mcp.tools.indicators as ind_mod

        monkeypatch.setattr(caches_mod, 'get_cached_scanner', lambda: FAKE_SCANNER_DATA)
        monkeypatch.setattr(ind_mod, 'backtest_indicator_formula', _make_fake_bt)

        with app.app_context():
            out = scan_and_backtest({
                'expression': 'RSI(14) < 35',
                'universe': 'all',
            })

        assert 'warning' in out
        assert 'Universe=all' in out['warning']
        assert 'no setup-quality filter' in out['warning'].lower() or 'no setup-quality' in out['warning']

    def test_verdict_is_none_in_response(self, app, monkeypatch):
        """universe=all sets verdict=None in the response (no filter applied)."""
        import carrac.caches as caches_mod
        import carrac.mcp.tools.indicators as ind_mod

        monkeypatch.setattr(caches_mod, 'get_cached_scanner', lambda: FAKE_SCANNER_DATA)
        monkeypatch.setattr(ind_mod, 'backtest_indicator_formula', _make_fake_bt)

        with app.app_context():
            out = scan_and_backtest({
                'expression': 'RSI(14) < 35',
                'universe': 'all',
            })

        assert out['verdict'] is None


# ── universe="scanner" ────────────────────────────────────────────

class TestUniverseScanner:
    def test_filters_to_verdict_assets(self, app, monkeypatch):
        """universe=scanner (default) applies verdict filter — only STRONG,GOOD assets."""
        from carrac.mcp.tools import scanner as scanner_mod
        import carrac.mcp.tools.indicators as ind_mod

        # Only STRONG/GOOD rows should pass through scanner_get_screen.
        strong_good = [r for r in FAKE_SCANNER_RESULTS
                       if r['best_verdict'] in ('STRONG', 'GOOD')]
        monkeypatch.setattr(
            scanner_mod, 'scanner_get_screen',
            lambda args: {'results': strong_good}
        )
        monkeypatch.setattr(ind_mod, 'backtest_indicator_formula', _make_fake_bt)

        with app.app_context():
            out = scan_and_backtest({
                'expression': 'RSI(14) < 35',
                'universe': 'scanner',
            })

        assert out['count'] <= 2  # at most STRONG + GOOD
        symbols = [r['symbol'] for r in out['results']]
        assert 'SOL' not in symbols
        assert 'ADA' not in symbols

    def test_no_warning_field(self, app, monkeypatch):
        """universe=scanner does NOT include a warning field."""
        from carrac.mcp.tools import scanner as scanner_mod
        import carrac.mcp.tools.indicators as ind_mod

        monkeypatch.setattr(
            scanner_mod, 'scanner_get_screen',
            lambda args: {'results': FAKE_SCANNER_RESULTS[:2]}
        )
        monkeypatch.setattr(ind_mod, 'backtest_indicator_formula', _make_fake_bt)

        with app.app_context():
            out = scan_and_backtest({
                'expression': 'RSI(14) < 35',
                'universe': 'scanner',
            })

        assert 'warning' not in out


# ── default behaviour (backward compat) ──────────────────────────

class TestDefaultUniverse:
    def test_omitting_universe_defaults_to_scanner(self, app, monkeypatch):
        """Omitting universe= defaults to 'scanner' — backward compatible."""
        from carrac.mcp.tools import scanner as scanner_mod
        import carrac.mcp.tools.indicators as ind_mod

        monkeypatch.setattr(
            scanner_mod, 'scanner_get_screen',
            lambda args: {'results': FAKE_SCANNER_RESULTS[:2]}
        )
        monkeypatch.setattr(ind_mod, 'backtest_indicator_formula', _make_fake_bt)

        with app.app_context():
            out = scan_and_backtest({'expression': 'RSI(14) < 35'})

        assert out['universe'] == 'scanner'
        assert 'warning' not in out

    def test_default_verdict_is_strong_good(self, app, monkeypatch):
        """Omitting both universe and verdict uses STRONG,GOOD filter."""
        from carrac.mcp.tools import scanner as scanner_mod
        import carrac.mcp.tools.indicators as ind_mod

        captured = {}

        def spy_scanner(args):
            captured['args'] = args
            return {'results': []}

        monkeypatch.setattr(scanner_mod, 'scanner_get_screen', spy_scanner)
        monkeypatch.setattr(ind_mod, 'backtest_indicator_formula', _make_fake_bt)

        with app.app_context():
            scan_and_backtest({'expression': 'RSI(14) < 35'})

        assert 'STRONG' in captured['args'].get('verdict', '')
        assert 'GOOD' in captured['args'].get('verdict', '')


# ── invalid universe ──────────────────────────────────────────────

class TestInvalidUniverse:
    def test_invalid_universe_raises_tool_error(self, app):
        """Invalid universe value raises ToolError listing valid options."""
        with app.app_context():
            with pytest.raises(ToolError) as exc_info:
                scan_and_backtest({'expression': 'RSI(14) < 35', 'universe': 'foo'})

        err = str(exc_info.value)
        assert 'foo' in err
        assert 'scanner' in err
        assert 'all' in err

    @pytest.mark.parametrize('bad_val', ['ALL', 'Scanner', 'FULL', '', ' '])
    def test_wrong_case_and_blanks_raise(self, app, bad_val):
        """Case variants and blank strings are all invalid."""
        with app.app_context():
            with pytest.raises(ToolError):
                scan_and_backtest({'expression': 'RSI(14) < 35', 'universe': bad_val})
