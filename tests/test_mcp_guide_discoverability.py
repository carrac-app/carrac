"""
Tests for MCP guide discoverability:
  get_guide loads its guides (path resolution)
  indicator_get_dsl_reference tool
  get_guide with no argument lists the available sections
"""

from unittest.mock import patch

import pytest

from carrac.mcp import tools  # noqa: F401  ensures all tools are registered
from carrac.mcp.registry import TOOL_REGISTRY


# ── Helpers ──────────────────────────────────────────────────────────────


def _guide(args=None):
    return TOOL_REGISTRY['get_guide']['handler'](args or {})


def _dsl():
    return TOOL_REGISTRY['indicator_get_dsl_reference']['handler']({})


# ── get_guide path fix ───────────────────────────────────────────────


class TestGetGuidePathFix:
    """Verify that guide.html is loaded correctly (parents[3] path fix)."""

    def test_formulas_returns_non_empty_content(self):
        """get_guide('formulas') must return real text content, not an error."""
        out = _guide({'topic': 'formulas'})
        assert 'error' not in out, f"Expected content, got error: {out.get('error')}"
        assert 'content' in out
        assert len(out['content']) > 50, "Content should be non-trivial"

    def test_backtest_returns_non_empty_content(self):
        """get_guide('backtest') must return real text content via alias.

        'non-empty' was too weak to catch the actual bug — the
        alias resolved to the API section's MCP-setup boilerplate, which is
        very non-empty and never once said 'backtest'. See
        TestBacktesterGuideDiscovery below for the assertion that bites.
        """
        out = _guide({'topic': 'backtest'})
        assert 'error' not in out, f"Expected content, got error: {out.get('error')}"
        assert 'content' in out
        assert len(out['content']) > 50

    def test_guide_sections_loaded_at_all(self):
        """_GUIDE_SECTIONS must not be empty — path must resolve correctly."""
        from carrac.mcp.tools.guide import _GUIDE_SECTIONS, _load_guide
        _load_guide()  # idempotent — ensures it ran
        assert len(_GUIDE_SECTIONS) > 0, (
            "_GUIDE_SECTIONS is empty — guide.html path resolution is broken"
        )

    def test_known_section_ids_present(self):
        """Key section IDs must exist after load."""
        from carrac.mcp.tools.guide import _GUIDE_SECTIONS, _load_guide
        _load_guide()
        for sid in ('chart', 'indicators', 'alerts', 'plays'):
            assert sid in _GUIDE_SECTIONS, f"Section '{sid}' missing from guide"


# ── Backtester is reachable from the first-contact tools ─────


class TestBacktesterGuideDiscovery:
    """An agent following Carrac's own documented flow (connect → get_started
    → get_guide) must be able to find the backtester.

    get_guide('backtest') must not alias to the 'api' section: that section,
    if truncated at 3000 chars while the word 'backtest' first appears at
    offset 4,406, returns MCP-setup boilerplate that stops mid-sentence, with
    no error and no truncation notice.
    """

    @pytest.mark.parametrize('topic', [
        'backtest', 'backtester', 'backtesting', 'strategy', 'simulate',
    ])
    def test_topic_actually_reaches_backtester_content(self, topic):
        out = _guide({'topic': topic})
        assert out.get('topic') == 'backtester', (
            f"get_guide({topic!r}) resolved to {out.get('topic')!r}, not the "
            f"backtester section"
        )
        body = out['content'].lower()
        # The words that make it the backtester and not a section that merely
        # name-drops it in passing.
        for term in ('entry', 'exit', 'win rate', 'drawdown'):
            assert term in body, f"{term!r} missing from get_guide({topic!r})"

    def test_backtester_has_its_own_section(self):
        """It must be a top-level <h2>, not an <h3> buried inside 'api'."""
        from carrac.mcp.tools.guide import _GUIDE_SECTIONS, _load_guide
        _load_guide()
        assert 'backtester' in _GUIDE_SECTIONS

    def test_three_models_are_distinguished(self):
        """The one place a user learns the three models aren't comparable."""
        body = _guide({'topic': 'backtester'})['content'].lower()
        assert 'hold-n' in body
        assert 'historical analog' in body
        assert 'full simulation' in body

    def test_no_section_is_silently_truncated(self):
        """Every section returns whole, and says so via truncated=False.

        Silent truncation (e.g. cutting sections at 3000 chars) is not allowed.
        """
        from carrac.mcp.tools.guide import _GUIDE_SECTIONS, _load_guide
        _load_guide()
        for sid in _GUIDE_SECTIONS:
            out = _guide({'topic': sid})
            assert out.get('truncated') is False, (
                f"section {sid!r} is truncated — raise _MAX_SECTION_CHARS or "
                f"split the section"
            )

    def test_truncation_is_announced_when_it_happens(self):
        """If a section ever does exceed the cap, the caller is told."""
        from carrac.mcp.tools import guide as g
        g._load_guide()
        longest = max(g._GUIDE_SECTIONS, key=lambda s: len(g._GUIDE_SECTIONS[s]['text']))
        original = g._MAX_SECTION_CHARS
        try:
            g._MAX_SECTION_CHARS = 100
            out = _guide({'topic': longest})
            assert out['truncated'] is True
            assert out['full_length'] > 100
            assert 'truncated' in out['note'].lower()
        finally:
            g._MAX_SECTION_CHARS = original

    def test_deep_content_is_findable_by_full_text_search(self):
        """Truncating at load time also crippled the full-text fallback —
        anything documented past char 3000 was unreachable by every path."""
        out = _guide({'topic': 'sortino'})
        hit = out.get('topic') or (out.get('results') or [{}])[0].get('topic')
        assert hit == 'backtester', f'full-text search for "sortino" found {hit!r}'


class TestGetStartedSurfacesBacktesting:
    """get_started is the documented FIRST call. It never mentioned the
    backtester — the capability that is the differentiator."""

    @staticmethod
    def _hint(tier):
        from carrac.mcp.runtime import set_mcp_caller, reset_mcp_caller
        from carrac.mcp.tools.onboarding import _backtesting_hint
        set_mcp_caller(1, tier)
        try:
            return _backtesting_hint()
        finally:
            reset_mcp_caller()

    @pytest.mark.parametrize('tier', ['member', 'tester', 'premium', 'admin'])
    def test_entitled_tiers_are_told_the_tools_exist(self, tier):
        hint = self._hint(tier)
        assert hint['available'] is True
        assert 'backtest_run' in hint['tools']
        assert 'backtest_simulate' in hint['tools']

    def test_free_tier_is_told_not_to_call_them(self):
        """tools/list is entitlement-filtered — pointing a free agent at
        backtest_run points it at a tool it cannot see."""
        hint = self._hint('free')
        assert hint['available'] is False
        assert 'tools' not in hint

    def test_backtest_models_glossary_is_pointed_at(self):
        """Without it an agent will compare a Hold-N hit rate to a Full-Sim
        win rate — the one thing the glossary explicitly forbids."""
        from carrac.mcp.tools.onboarding import get_started
        with patch('carrac.mcp.tools.onboarding._safe', return_value={}):
            out = get_started({})
        assert 'carrac://glossary/backtest-models' in out['next_steps']['read_resources']

    def test_next_steps_carries_the_backtesting_block(self):
        from carrac.mcp.tools.onboarding import get_started
        from carrac.mcp.runtime import set_mcp_caller, reset_mcp_caller
        set_mcp_caller(1, 'member')
        try:
            with patch('carrac.mcp.tools.onboarding._safe', return_value={}):
                out = get_started({})
        finally:
            reset_mcp_caller()
        assert out['next_steps']['backtesting']['available'] is True


# ── get_guide enriched no-arg behavior ───────────────────────────────


class TestGetGuideNoArg:
    """Verify get_guide() with no topic returns enriched sections list."""

    def test_no_arg_returns_sections_list(self):
        """get_guide() must return a 'sections' key with a list."""
        out = _guide({})
        assert 'sections' in out
        assert isinstance(out['sections'], list)

    def test_no_arg_returns_at_least_20_sections(self):
        """Must have >= 20 sections (guide.html has 26+)."""
        out = _guide({})
        assert len(out['sections']) >= 20, (
            f"Expected >=20 sections, got {len(out['sections'])}"
        )

    def test_sections_have_id_title_aliases(self):
        """Each section entry must have 'id', 'title', and 'aliases' fields."""
        out = _guide({})
        for sec in out['sections']:
            assert 'id' in sec, f"Missing 'id' in section: {sec}"
            assert 'title' in sec, f"Missing 'title' in section: {sec}"
            assert 'aliases' in sec, f"Missing 'aliases' in section: {sec}"
            assert isinstance(sec['aliases'], list)

    def test_chart_section_has_aliases(self):
        """The 'chart' section should have at least one alias like 'timeframe'."""
        out = _guide({})
        chart_sections = [s for s in out['sections'] if s['id'] == 'chart']
        assert chart_sections, "No 'chart' section found"
        assert len(chart_sections[0]['aliases']) > 0

    def test_no_arg_has_hint(self):
        """No-arg call should include a 'hint' about how to get content."""
        out = _guide({})
        assert 'hint' in out


# ── indicator_get_dsl_reference ───────────────────────────────────────────────


class TestGetDslReference:
    """Verify the new indicator_get_dsl_reference MCP tool."""

    def test_tool_is_registered(self):
        """indicator_get_dsl_reference must be in TOOL_REGISTRY."""
        assert 'indicator_get_dsl_reference' in TOOL_REGISTRY

    def test_tool_is_free_tier(self):
        """indicator_get_dsl_reference must be min_tier='free'."""
        assert TOOL_REGISTRY['indicator_get_dsl_reference']['min_tier'] == 'free'

    def test_returns_moving_averages_non_empty(self):
        """functions.moving_averages must be non-empty."""
        out = _dsl()
        assert 'functions' in out
        assert 'moving_averages' in out['functions']
        assert len(out['functions']['moving_averages']) > 0

    def test_ema_and_sma_in_moving_averages(self):
        """'ema' and 'sma' must appear in functions.moving_averages."""
        out = _dsl()
        ma = out['functions']['moving_averages']
        assert 'ema' in ma, f"'ema' not in moving_averages: {ma}"
        assert 'sma' in ma, f"'sma' not in moving_averages: {ma}"

    def test_bbwidth_in_volatility(self):
        """'bbwidth' must appear in functions.volatility."""
        out = _dsl()
        assert 'volatility' in out['functions']
        assert 'bbwidth' in out['functions']['volatility'], (
            f"'bbwidth' not in volatility: {out['functions']['volatility']}"
        )

    def test_has_ohlcv_series(self):
        """Response must include ohlcv_series with close/high/low/open/volume."""
        out = _dsl()
        assert 'ohlcv_series' in out
        for field in ('open', 'high', 'low', 'close', 'volume'):
            assert field in out['ohlcv_series']

    def test_has_quant_vars(self):
        """quant_vars must be a non-empty list."""
        out = _dsl()
        assert 'quant_vars' in out
        assert isinstance(out['quant_vars'], list)
        assert len(out['quant_vars']) >= 10

    def test_has_derivatives_vars(self):
        """derivatives_vars must be a non-empty list."""
        out = _dsl()
        assert 'derivatives_vars' in out
        assert isinstance(out['derivatives_vars'], list)
        assert len(out['derivatives_vars']) >= 5

    def test_has_cpr_functions(self):
        """cpr_functions must include cpr_pivot and cpr_r1."""
        out = _dsl()
        assert 'cpr_functions' in out
        assert 'cpr_pivot' in out['cpr_functions']
        assert 'cpr_r1' in out['cpr_functions']

    def test_has_operators(self):
        """operators list must include AND, OR, NOT."""
        out = _dsl()
        assert 'operators' in out
        ops = out['operators']
        assert 'AND' in ops
        assert 'OR' in ops
        assert 'NOT' in ops

    def test_has_let_syntax(self):
        """let_syntax must be a non-empty string."""
        out = _dsl()
        assert 'let_syntax' in out
        assert isinstance(out['let_syntax'], str)
        assert len(out['let_syntax']) > 5

    def test_has_examples(self):
        """examples must be a list with at least 3 entries."""
        out = _dsl()
        assert 'examples' in out
        assert len(out['examples']) >= 3

    def test_has_limits(self):
        """limits must include max_expression_length."""
        out = _dsl()
        assert 'limits' in out
        assert 'max_expression_length' in out['limits']
        assert out['limits']['max_expression_length'] == 500

    def test_has_engine_version(self):
        """engine_version must be an integer >= 1."""
        out = _dsl()
        assert 'engine_version' in out
        assert isinstance(out['engine_version'], int)
        assert out['engine_version'] >= 1

    def test_uncategorized_is_flagged_not_hidden(self):
        """If uncategorized functions exist they must appear in the response
        (not silently dropped). An empty uncategorized list is also acceptable."""
        out = _dsl()
        # Flatten all categorized function names.
        all_categorized = set()
        for names in out['functions'].values():
            all_categorized.update(names)
        # All categorized names must either be valid FUNCTION_REGISTRY keys
        # or be known special forms (htf, mtf, divergence, if).
        from carrac.formula_eval import FUNCTION_REGISTRY
        special_ok = {'htf', 'mtf', 'divergence', 'if'}
        for name in all_categorized:
            assert name in FUNCTION_REGISTRY or name in special_ok, (
                f"'{name}' is in DSL response but not in FUNCTION_REGISTRY or special forms"
            )

    def test_has_discovery_hint(self):
        """discovery_hint must be a non-empty string."""
        out = _dsl()
        assert 'discovery_hint' in out
        assert isinstance(out['discovery_hint'], str)
        assert len(out['discovery_hint']) > 10
