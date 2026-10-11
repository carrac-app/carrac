"""Indicator registry tests:
  - indicator_list warning + diagnostics when registry is empty
  - fuzzy lookup for indicator_get_definition / get_indicator_fuzzy
"""

from unittest.mock import patch

from carrac.mcp import tools  # noqa: F401  ensures registration
from carrac.mcp.registry import TOOL_REGISTRY
from carrac.mcp.indicator_registry_parser import (
    get_indicator_fuzzy,
    get_registry_diagnostics,
)


# ── fuzzy lookup ────────────────────────────────────────────────────────

class TestGetIndicatorFuzzy:
    def test_volume_by_label_returns_vol_entry(self):
        """'Volume' label should match the VOL registry key."""
        result = get_indicator_fuzzy('Volume')
        assert 'error' not in result, f"Expected match, got: {result}"
        assert result['name'] == 'VOL'

    def test_bollinger_bands_returns_boll_entry(self):
        """'Bollinger Bands' should fuzzy-match to BOLL."""
        result = get_indicator_fuzzy('Bollinger Bands')
        assert 'error' not in result, f"Expected match, got: {result}"
        assert result['name'] == 'BOLL'

    def test_atr_returns_atr_pct_entry(self):
        """'ATR' is not a direct key; ATR_PCT is the canonical entry.
        Tier 3 substring match: 'atr' is in 'atr %' (ATR_PCT label)."""
        result = get_indicator_fuzzy('ATR')
        assert 'error' not in result, f"Expected match, got: {result}"
        assert result['name'] == 'ATR_PCT'

    def test_exact_key_lookup_still_works(self):
        """Direct key lookup (Tier 1) must still work after fuzzy fallback is added."""
        result = get_indicator_fuzzy('RSI')
        assert 'error' not in result
        assert result['name'] == 'RSI'

    def test_exact_key_case_insensitive(self):
        result = get_indicator_fuzzy('rsi')
        assert 'error' not in result
        assert result['name'] == 'RSI'

    def test_nonsense_returns_error_with_suggestions(self):
        """Unknown inputs must return error dict with top-3 suggestions."""
        result = get_indicator_fuzzy('nonsense_indicator')
        assert 'error' in result
        assert 'suggestions' in result
        assert isinstance(result['suggestions'], list)
        assert len(result['suggestions']) == 3
        for s in result['suggestions']:
            assert 'key' in s
            assert 'label' in s

    def test_suggestions_contain_valid_keys(self):
        """Suggestions keys must all exist in the registry."""
        from carrac.mcp.indicator_registry_parser import get_all_indicators
        all_ind = get_all_indicators()
        result = get_indicator_fuzzy('zzznomatch9999')
        for s in result.get('suggestions', []):
            assert s['key'] in all_ind, f"Suggestion key {s['key']} not in registry"

    def test_cpr_exact_key_works(self):
        """CPR is a direct key — must return immediately from Tier 1."""
        result = get_indicator_fuzzy('CPR')
        assert 'error' not in result
        assert result['name'] == 'CPR'

    def test_typo_does_not_hallucinate_unrelated_indicator(self):
        """'Boilinger Bandz' (typo of 'Bollinger Bands') must not
        return OI (Open Interest) with full confidence — a coincidental
        substring match ('oi' inside 'boilinger') with no similarity floor.
        It must never return an unrelated indicator; either a proper
        error+suggestions, or (best case) the real Bollinger match."""
        result = get_indicator_fuzzy('Boilinger Bandz')
        if 'error' in result:
            assert 'suggestions' in result
        else:
            assert result['name'] != 'OI', (
                'Must not confidently return Open Interest for a Bollinger typo'
            )

    def test_garbled_string_does_not_hallucinate_via_short_label_collision(self):
        """'xzqwmapqzx' coincidentally contains 'ma' (MA's
        shortLabel) and must not return the MA indicator with full
        confidence. Must fall through to suggestions instead."""
        result = get_indicator_fuzzy('xzqwmapqzx')
        assert 'error' in result, f'Expected no-match, got confident result: {result}'
        assert 'suggestions' in result

    def test_short_acronym_substring_collision_rejected(self):
        """A short shortLabel (e.g. 'OI') coincidentally appearing inside an
        unrelated longer string must not be treated as a confident match."""
        result = get_indicator_fuzzy('somerandomtextcontainingoiinit')
        if 'error' not in result:
            assert result['name'] != 'OI'


# ── tool handler fuzzy lookup ───────────────────────────────────────────

class TestGetIndicatorDefinitionFuzzy:
    def test_volume_returns_vol(self):
        fn = TOOL_REGISTRY['indicator_get_definition']['handler']
        out = fn({'name': 'Volume'})
        assert 'error' not in out
        assert out['name'] == 'VOL'

    def test_bollinger_bands_returns_boll(self):
        fn = TOOL_REGISTRY['indicator_get_definition']['handler']
        out = fn({'name': 'Bollinger Bands'})
        assert 'error' not in out
        assert out['name'] == 'BOLL'

    def test_atr_returns_atr_pct(self):
        fn = TOOL_REGISTRY['indicator_get_definition']['handler']
        out = fn({'name': 'ATR'})
        assert 'error' not in out
        assert out['name'] == 'ATR_PCT'

    def test_unknown_returns_error_and_suggestions(self):
        fn = TOOL_REGISTRY['indicator_get_definition']['handler']
        out = fn({'name': 'nonsense_indicator'})
        assert 'error' in out
        assert 'suggestions' in out
        assert len(out['suggestions']) == 3

    def test_near_miss_does_not_return_unrelated_indicator(self):
        """Tool-level pin against hallucinated matches: a
        garbled/near-miss name must never come back as a confident,
        unrelated indicator definition."""
        fn = TOOL_REGISTRY['indicator_get_definition']['handler']
        out = fn({'name': 'Boilinger Bandz'})
        if 'error' not in out:
            assert out['name'] != 'OI'
        out2 = fn({'name': 'xzqwmapqzx'})
        assert 'error' in out2
        assert 'suggestions' in out2


# ── indicator_list warning when registry empty ─────────────────────────

class TestListIndicatorsWarningOnEmpty:
    def test_warning_field_present_when_registry_empty(self):
        """When get_all_indicators() returns {}, indicator_list must include
        a 'warning' field and a 'diagnostics' field.

        The functions are imported locally inside indicator_list, so we patch
        at the source module level (indicator_registry_parser).
        """
        fn = TOOL_REGISTRY['indicator_list']['handler']
        fake_diag = {
            'path': '/fake/path',
            'exists': False,
            'mtime': None,
            'count': 0,
            'stale': False,
        }
        with patch(
            'carrac.mcp.indicator_registry_parser.get_all_indicators',
            return_value={},
        ), patch(
            'carrac.mcp.indicator_registry_parser.get_categories',
            return_value=[],
        ), patch(
            'carrac.mcp.indicator_registry_parser.get_registry_diagnostics',
            return_value=fake_diag,
        ):
            out = fn({})

        assert out['count'] == 0
        assert 'warning' in out, "Must include 'warning' key when registry is empty"
        assert 'registry' in out['warning'].lower() or '0' in out['warning']
        assert 'diagnostics' in out
        assert 'path' in out['diagnostics']
        assert 'exists' in out['diagnostics']

    def test_no_warning_when_registry_has_entries(self):
        """Normal operation: no 'warning' key in the response."""
        fn = TOOL_REGISTRY['indicator_list']['handler']
        out = fn({})
        assert out['count'] >= 50
        assert 'warning' not in out


# ── get_registry_diagnostics ────────────────────────────────────────────

class TestRegistryDiagnostics:
    def test_diagnostics_shape(self):
        diag = get_registry_diagnostics()
        for key in ('path', 'exists', 'mtime', 'count', 'stale'):
            assert key in diag, f"Missing key: {key}"

    def test_diagnostics_path_is_string(self):
        diag = get_registry_diagnostics()
        assert isinstance(diag['path'], str)

    def test_diagnostics_count_matches_registry(self):
        from carrac.mcp.indicator_registry_parser import get_all_indicators
        diag = get_registry_diagnostics()
        assert diag['count'] == len(get_all_indicators())

    def test_not_stale_after_fresh_parse(self):
        """After a fresh parse the stale flag should be False."""
        diag = get_registry_diagnostics()
        assert diag['stale'] is False
