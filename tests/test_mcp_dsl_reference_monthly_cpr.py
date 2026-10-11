"""
Tests for the DSL reference and guide coverage of monthly CPR:
  indicator_get_dsl_reference includes monthly CPR functions + updated examples + discovery_hint
  guide.html custom-formulas section mentions MONTHLY_CPR_P
"""


from carrac.mcp import tools  # noqa: F401  ensures all tools are registered
from carrac.mcp.registry import TOOL_REGISTRY


# ── Helpers ──────────────────────────────────────────────────────────────


def _dsl():
    return TOOL_REGISTRY['indicator_get_dsl_reference']['handler']({})


def _guide(args=None):
    return TOOL_REGISTRY['get_guide']['handler'](args or {})


# ── indicator_get_dsl_reference monthly CPR additions ─────────────────────────


class TestDslReferenceMonthlyAnchoredCpr:
    """Verify that indicator_get_dsl_reference includes the new monthly CPR functions."""

    def test_cpr_functions_contains_monthly_cpr_p(self):
        """cpr_functions must include 'monthly_cpr_p'."""
        out = _dsl()
        assert 'cpr_functions' in out
        assert 'monthly_cpr_p' in out['cpr_functions'], (
            f"'monthly_cpr_p' missing from cpr_functions: {out['cpr_functions']}"
        )

    def test_cpr_functions_contains_monthly_pivot_alias(self):
        """cpr_functions must include 'monthly_pivot' (alias for monthly_cpr_p)."""
        out = _dsl()
        assert 'monthly_pivot' in out['cpr_functions'], (
            f"'monthly_pivot' missing from cpr_functions: {out['cpr_functions']}"
        )

    def test_cpr_functions_contains_monthly_tc_and_bc(self):
        """cpr_functions must include monthly_cpr_tc and monthly_cpr_bc."""
        out = _dsl()
        cpr = out['cpr_functions']
        assert 'monthly_cpr_tc' in cpr, f"'monthly_cpr_tc' missing: {cpr}"
        assert 'monthly_cpr_bc' in cpr, f"'monthly_cpr_bc' missing: {cpr}"

    def test_cpr_functions_contains_monthly_resistance_levels(self):
        """cpr_functions must include monthly_cpr_r1, r2, r3."""
        out = _dsl()
        cpr = out['cpr_functions']
        for name in ('monthly_cpr_r1', 'monthly_cpr_r2', 'monthly_cpr_r3'):
            assert name in cpr, f"'{name}' missing from cpr_functions: {cpr}"

    def test_cpr_functions_contains_monthly_support_levels(self):
        """cpr_functions must include monthly_cpr_s1, s2, s3."""
        out = _dsl()
        cpr = out['cpr_functions']
        for name in ('monthly_cpr_s1', 'monthly_cpr_s2', 'monthly_cpr_s3'):
            assert name in cpr, f"'{name}' missing from cpr_functions: {cpr}"

    def test_cpr_functions_still_has_rolling_daily_variants(self):
        """cpr_functions must still include the original rolling daily cpr_pivot, cpr_r1, etc."""
        out = _dsl()
        cpr = out['cpr_functions']
        for name in ('cpr_pivot', 'cpr_r1', 'cpr_s1'):
            assert name in cpr, f"Rolling daily '{name}' was unexpectedly removed: {cpr}"

    def test_monthly_cpr_functions_not_in_uncategorized(self):
        """monthly_cpr_* functions must NOT appear in functions['uncategorized']."""
        out = _dsl()
        uncategorized = out.get('functions', {}).get('uncategorized', [])
        for name in uncategorized:
            assert not name.startswith('monthly_cpr') and name != 'monthly_pivot', (
                f"'{name}' ended up in uncategorized — it should be in cpr_functions"
            )


class TestDslReferenceExamplesMonthly:
    """Verify the new monthly CPR and 1M HTF examples are present."""

    def test_examples_contains_monthly_cpr_p_expression(self):
        """examples list must contain a string with 'MONTHLY_CPR_P()'."""
        out = _dsl()
        examples = out.get('examples', [])
        assert any('MONTHLY_CPR_P()' in ex for ex in examples), (
            f"No example with MONTHLY_CPR_P() found in: {examples}"
        )

    def test_examples_contains_htf_1m_expression(self):
        """examples list must contain a string with HTF('1M', ...) resampling."""
        out = _dsl()
        examples = out.get('examples', [])
        assert any("HTF('1M'" in ex for ex in examples), (
            f"No example with HTF('1M', ...) found in: {examples}"
        )


class TestDslReferenceDiscoveryHintMonthly:
    """Verify the discovery_hint mentions monthly-anchored CPR."""

    def test_discovery_hint_mentions_monthly_anchored(self):
        """discovery_hint must reference monthly-anchored CPR variant."""
        out = _dsl()
        hint = out.get('discovery_hint', '')
        # Must mention the monthly-anchored concept
        assert 'monthly' in hint.lower(), (
            f"discovery_hint does not mention 'monthly': {hint!r}"
        )

    def test_discovery_hint_contrasts_two_cpr_variants(self):
        """discovery_hint should mention both rolling daily and monthly-anchored CPR."""
        out = _dsl()
        hint = out.get('discovery_hint', '').lower()
        # Should reference both concepts so a caller can distinguish them
        assert 'cpr_pivot' in hint or 'rolling' in hint or 'daily' in hint, (
            f"discovery_hint does not mention rolling/daily CPR: {hint!r}"
        )
        assert 'monthly' in hint, (
            f"discovery_hint does not mention monthly CPR: {hint!r}"
        )


# ── guide.html formula section includes MONTHLY_CPR_P ───────────────


class TestGuideMonthlyAnchoredCpr:
    """Verify that the custom-formulas guide section mentions monthly CPR."""

    def test_formulas_guide_mentions_monthly_cpr_p(self):
        """get_guide('formulas') content must contain MONTHLY_CPR_P (case-insensitive)."""
        out = _guide({'topic': 'formulas'})
        assert 'error' not in out, f"Guide returned error: {out.get('error')}"
        content = out.get('content', '')
        assert 'monthly_cpr_p' in content.lower(), (
            f"'MONTHLY_CPR_P' not found in formulas guide content (first 500 chars): {content[:500]!r}"
        )

    def test_formulas_guide_mentions_monthly_cpr_tc(self):
        """get_guide('formulas') content must also mention monthly_cpr_tc."""
        out = _guide({'topic': 'formulas'})
        content = out.get('content', '')
        assert 'monthly_cpr_tc' in content.lower(), (
            "'MONTHLY_CPR_TC' not found in formulas guide content"
        )

    def test_formulas_guide_contrasts_rolling_and_anchored(self):
        """Guide must explain that cpr_pivot and monthly_cpr_p are different concepts."""
        out = _guide({'topic': 'formulas'})
        content = out.get('content', '').lower()
        # Should mention both cpr_pivot (rolling) and monthly anchored
        assert 'cpr_pivot' in content, "Guide missing cpr_pivot rolling daily mention"
        assert 'monthly' in content, "Guide missing monthly anchored CPR mention"
