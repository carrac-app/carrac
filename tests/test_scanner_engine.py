"""
Tests for: scanner hard-cap vetoes, plus the funding-rate + smart-money-bias score MODIFIERS
(TestScoreModifiers below) — bounded ±1 nudges to the 0-12 total, applied
after vetoes and before verdict classification.
"""

import contextlib
import os
import sys
from unittest.mock import patch


sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from carrac.scanner.scorer.engine import score_asset, score_all_assets


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_asset_data(trend_score=0, all_opposed=True, rsi_score=2,
                     momentum_score=2, structure_score=2, level_score=2):
    """Build minimal asset_data + mock criteria via patching scorer criteria functions."""
    return {
        'symbol': 'TESTCOIN',
        'latest_price': 100.0,
        'consensio': {},
        'ema_ribbon': {},
    }


def _make_macro_data(regime='Neutral', fear_greed=50, hard_zero_long=False, hard_zero_short=False):
    return {
        'regime': regime,
        'regime_score': 5,
        'fear_greed': fear_greed,
        'breadth': {},
    }


# ---------------------------------------------------------------------------
# Test 1: all-opposed TFs veto → score CAPPED, not inflated
# ---------------------------------------------------------------------------

class TestAllOpposedVeto:
    """With all TFs opposed the total must be capped at WEAK."""

    def _patched_criteria(self):
        """Return mocked criterion functions that simulate all-TFs-opposed scenario."""

        # trend: score=0, all_opposed=True
        # macro: score=2 (neutral — no macro veto)
        # all others: score=3 each → total without trend = 2+3+3+3+3 = 14
        # veto should cap at 5 (WEAK)
        def fake_trend(asset_data, direction):
            return 0, ['all TFs opposed'], {'all_opposed': True}

        def fake_momentum(asset_data, direction):
            return 3, ['momentum ok'], {}

        def fake_rsi(asset_data, direction):
            return 3, ['rsi ok'], {'exhaustion': False}

        def fake_structure(asset_data, direction):
            return 3, ['structure ok'], {}

        def fake_level(asset_data, direction):
            return 3, ['level ok'], {}

        def fake_macro(macro_data, direction):
            return 2, ['macro neutral'], {'hard_zero_long': False, 'hard_zero_short': False}

        return {
            'carrac.scanner.scorer.engine._score_trend': fake_trend,
            'carrac.scanner.scorer.engine._score_momentum': fake_momentum,
            'carrac.scanner.scorer.engine._score_rsi': fake_rsi,
            'carrac.scanner.scorer.engine._score_structure': fake_structure,
            'carrac.scanner.scorer.engine._score_level': fake_level,
            'carrac.scanner.scorer.engine._score_macro': fake_macro,
        }

    def test_veto_caps_total(self):
        """all-opposed veto: total must be <= 5 though the other criteria sum to 14."""
        from unittest.mock import patch

        asset_data = _make_asset_data()
        macro_data = _make_macro_data()

        patches = self._patched_criteria()
        with patch('carrac.scanner.scorer.engine._score_trend', patches['carrac.scanner.scorer.engine._score_trend']), \
             patch('carrac.scanner.scorer.engine._score_momentum', patches['carrac.scanner.scorer.engine._score_momentum']), \
             patch('carrac.scanner.scorer.engine._score_rsi', patches['carrac.scanner.scorer.engine._score_rsi']), \
             patch('carrac.scanner.scorer.engine._score_structure', patches['carrac.scanner.scorer.engine._score_structure']), \
             patch('carrac.scanner.scorer.engine._score_level', patches['carrac.scanner.scorer.engine._score_level']), \
             patch('carrac.scanner.scorer.engine._score_macro', patches['carrac.scanner.scorer.engine._score_macro']), \
             patch('carrac.scanner.confluence.compute_confluences', return_value=None), \
             patch('carrac.scanner.scorer.setups.classify_setup', return_value=None):

            result = score_asset(asset_data, macro_data, 'long')

        assert result['total'] <= 5, (
            f"All-opposed veto should cap the score at 5, got {result['total']}."
        )
        assert any('opposed' in v.lower() for v in result.get('vetoes', [])), (
            "Expected 'all opposed' veto in result vetoes"
        )


# ---------------------------------------------------------------------------
# Test 2: funding-rate + smart-money-bias score modifiers
# ---------------------------------------------------------------------------

def _fixed_criteria(scores, all_opposed=False, hard_zero_long=False,
                     hard_zero_short=False, exhaustion=False):
    """Build fake criterion functions returning a fixed score regardless of
    direction, with vetoes/exhaustion off by default so only the modifier
    under test moves the total."""

    def fake_trend(asset_data, direction):
        return scores['trend'], ['trend'], {'all_opposed': all_opposed}

    def fake_momentum(asset_data, direction):
        return scores['momentum'], ['momentum'], {}

    def fake_rsi(asset_data, direction):
        return scores['rsi'], ['rsi'], {'exhaustion': exhaustion}

    def fake_structure(asset_data, direction):
        return scores['structure'], ['structure'], {}

    def fake_level(asset_data, direction):
        return scores['level'], ['level'], {}

    def fake_macro(macro_data, direction):
        return scores['macro'], ['macro'], {
            'hard_zero_long': hard_zero_long, 'hard_zero_short': hard_zero_short,
        }

    return fake_trend, fake_momentum, fake_rsi, fake_structure, fake_level, fake_macro


@contextlib.contextmanager
def _patched_engine(fakes):
    """Patch all 6 criterion functions (as looked up in engine.py) plus the
    setup/confluence side calls that score_asset always attempts."""
    fake_trend, fake_momentum, fake_rsi, fake_structure, fake_level, fake_macro = fakes
    with patch('carrac.scanner.scorer.engine._score_trend', fake_trend), \
         patch('carrac.scanner.scorer.engine._score_momentum', fake_momentum), \
         patch('carrac.scanner.scorer.engine._score_rsi', fake_rsi), \
         patch('carrac.scanner.scorer.engine._score_structure', fake_structure), \
         patch('carrac.scanner.scorer.engine._score_level', fake_level), \
         patch('carrac.scanner.scorer.engine._score_macro', fake_macro), \
         patch('carrac.scanner.confluence.compute_confluences', return_value=None), \
         patch('carrac.scanner.scorer.setups.classify_setup', return_value=None):
        yield


class TestScoreModifiers:
    """Funding-rate (contrarian) + smart-money-bias modifiers."""

    def test_extreme_positive_funding_flips_long_down_and_short_up(self):
        """Long baseline 10 -> 9 (crowded longs fades long); short baseline
        4 -> 5 (contrarian boost). Evidence must be present on both."""
        macro_data = _make_macro_data()

        # long: 2+2+2+2+1+1 = 10
        long_scores = {'trend': 2, 'momentum': 2, 'rsi': 2, 'structure': 2, 'level': 1, 'macro': 1}
        # short: 0+1+1+1+1+0 = 4
        short_scores = {'trend': 0, 'momentum': 1, 'rsi': 1, 'structure': 1, 'level': 1, 'macro': 0}

        asset_data = {
            'symbol': 'TESTCOIN', 'latest_price': 100.0, 'consensio': {}, 'ema_ribbon': {},
            'funding_rate': 0.00081,  # 0.081%/8h, extreme positive
        }

        with _patched_engine(_fixed_criteria(long_scores)):
            long_result = score_asset(asset_data, macro_data, 'long')
        with _patched_engine(_fixed_criteria(short_scores)):
            short_result = score_asset(asset_data, macro_data, 'short')

        assert long_result['total'] == 9, long_result
        assert short_result['total'] == 5, short_result

        assert any('Funding' in m and 'crowded longs' in m and '-1' in m
                   for m in long_result['modifiers']), long_result['modifiers']
        assert any('Funding' in m and 'crowded longs' in m and '+1' in m
                   for m in short_result['modifiers']), short_result['modifiers']

    def test_extreme_negative_funding_flips_direction_opposite_way(self):
        """Crowded shorts (funding <= -0.05%): long +1, short -1."""
        macro_data = _make_macro_data()
        scores = {'trend': 1, 'momentum': 1, 'rsi': 1, 'structure': 1, 'level': 1, 'macro': 1}
        asset_data = {
            'symbol': 'TESTCOIN', 'latest_price': 100.0, 'consensio': {}, 'ema_ribbon': {},
            'funding_rate': -0.0009,  # -0.09%/8h, extreme negative
        }

        with _patched_engine(_fixed_criteria(scores)):
            long_result = score_asset(asset_data, macro_data, 'long')
        with _patched_engine(_fixed_criteria(scores)):
            short_result = score_asset(asset_data, macro_data, 'short')

        assert long_result['total'] == 7, long_result   # 6 baseline +1
        assert short_result['total'] == 5, short_result  # 6 baseline -1
        assert any('crowded shorts' in m for m in long_result['modifiers'])
        assert any('crowded shorts' in m for m in short_result['modifiers'])

    def test_smart_money_bias_80_long_plus_one(self):
        """Long-skewed smart money (bias>=70): long +1, short -1."""
        macro_data = _make_macro_data()
        scores = {'trend': 1, 'momentum': 1, 'rsi': 1, 'structure': 1, 'level': 1, 'macro': 1}
        asset_data = {
            'symbol': 'TESTCOIN', 'latest_price': 100.0, 'consensio': {}, 'ema_ribbon': {},
            'smart_money_bias': 83,
        }

        with _patched_engine(_fixed_criteria(scores)):
            long_result = score_asset(asset_data, macro_data, 'long')
        with _patched_engine(_fixed_criteria(scores)):
            short_result = score_asset(asset_data, macro_data, 'short')

        assert long_result['total'] == 7, long_result   # 6 baseline +1
        assert short_result['total'] == 5, short_result  # 6 baseline -1
        assert 'Smart money 83 long-skewed (+1)' in long_result['modifiers']
        assert 'Smart money 83 long-skewed (-1)' in short_result['modifiers']

    def test_smart_money_bias_20_short_skewed(self):
        """Short-skewed smart money (bias<=30): long -1, short +1."""
        macro_data = _make_macro_data()
        scores = {'trend': 1, 'momentum': 1, 'rsi': 1, 'structure': 1, 'level': 1, 'macro': 1}
        asset_data = {
            'symbol': 'TESTCOIN', 'latest_price': 100.0, 'consensio': {}, 'ema_ribbon': {},
            'smart_money_bias': 17,
        }

        with _patched_engine(_fixed_criteria(scores)):
            long_result = score_asset(asset_data, macro_data, 'long')
        with _patched_engine(_fixed_criteria(scores)):
            short_result = score_asset(asset_data, macro_data, 'short')

        assert long_result['total'] == 5, long_result
        assert short_result['total'] == 7, short_result
        assert 'Smart money 17 short-skewed (-1)' in long_result['modifiers']
        assert 'Smart money 17 short-skewed (+1)' in short_result['modifiers']

    def test_missing_data_is_identical_to_baseline(self):
        """No funding_rate / smart_money_bias keys at all -> no modifier,
        no evidence, total/verdict unchanged from the plain 6-criterion sum."""
        macro_data = _make_macro_data()
        scores = {'trend': 2, 'momentum': 1, 'rsi': 2, 'structure': 1, 'level': 1, 'macro': 1}
        asset_data = {'symbol': 'TESTCOIN', 'latest_price': 100.0, 'consensio': {}, 'ema_ribbon': {}}

        with _patched_engine(_fixed_criteria(scores)):
            result = score_asset(asset_data, macro_data, 'long')

        assert result['total'] == 8
        assert result['modifiers'] == []

    def test_none_values_are_identical_to_baseline(self):
        """Explicit None funding_rate/smart_money_bias behave the same as
        the keys being absent (precompute only sets the key when non-None,
        but a defensive None must also no-op, never raise)."""
        macro_data = _make_macro_data()
        scores = {'trend': 2, 'momentum': 1, 'rsi': 2, 'structure': 1, 'level': 1, 'macro': 1}
        asset_data = {
            'symbol': 'TESTCOIN', 'latest_price': 100.0, 'consensio': {}, 'ema_ribbon': {},
            'funding_rate': None, 'smart_money_bias': None,
        }

        with _patched_engine(_fixed_criteria(scores)):
            result = score_asset(asset_data, macro_data, 'long')

        assert result['total'] == 8
        assert result['modifiers'] == []

    def test_non_numeric_funding_and_bias_are_ignored_not_raised(self):
        """Type-guard: a stray string value must not raise and must not
        modify the score (defensive — precompute always stores floats)."""
        macro_data = _make_macro_data()
        scores = {'trend': 2, 'momentum': 1, 'rsi': 2, 'structure': 1, 'level': 1, 'macro': 1}
        asset_data = {
            'symbol': 'TESTCOIN', 'latest_price': 100.0, 'consensio': {}, 'ema_ribbon': {},
            'funding_rate': 'n/a', 'smart_money_bias': 'unknown',
        }

        with _patched_engine(_fixed_criteria(scores)):
            result = score_asset(asset_data, macro_data, 'long')

        assert result['total'] == 8
        assert result['modifiers'] == []

    def test_clamped_at_12_when_modifier_would_overflow(self):
        """Baseline total 12 (max) + a favorable modifier must stay at 12,
        never exceed the 0-12 scale."""
        macro_data = _make_macro_data()
        scores = {'trend': 2, 'momentum': 2, 'rsi': 2, 'structure': 2, 'level': 2, 'macro': 2}
        asset_data = {
            'symbol': 'TESTCOIN', 'latest_price': 100.0, 'consensio': {}, 'ema_ribbon': {},
            'funding_rate': -0.001,  # crowded shorts -> long +1 would overflow
            'smart_money_bias': 90,  # long-skewed -> long +1 would also overflow
        }

        with _patched_engine(_fixed_criteria(scores)):
            result = score_asset(asset_data, macro_data, 'long')

        assert result['total'] == 12
        assert len(result['modifiers']) == 2

    def test_clamped_at_0_when_modifier_would_underflow(self):
        """Baseline total 0 (min, no vetoes triggered) + an unfavorable
        modifier must stay at 0, never go negative."""
        macro_data = _make_macro_data()
        scores = {'trend': 0, 'momentum': 0, 'rsi': 0, 'structure': 0, 'level': 0, 'macro': 0}
        asset_data = {
            'symbol': 'TESTCOIN', 'latest_price': 100.0, 'consensio': {}, 'ema_ribbon': {},
            'funding_rate': 0.001,  # crowded longs -> long -1 would underflow
            'smart_money_bias': 5,  # short-skewed -> long -1 would also underflow
        }

        with _patched_engine(_fixed_criteria(scores, all_opposed=False)):
            result = score_asset(asset_data, macro_data, 'long')

        assert result['total'] == 0
        assert len(result['modifiers']) == 2

    def test_hard_veto_cap_survives_favorable_modifier(self):
        """A hard-cap veto (all TFs opposed) still wins even if a favorable
        modifier would otherwise push the total back above 5."""
        macro_data = _make_macro_data()
        # trend=0/all_opposed -> veto caps total at 5; other criteria max out
        # baseline (uncapped) would be 0+2+2+2+2+2=10, capped to 5.
        scores = {'trend': 0, 'momentum': 2, 'rsi': 2, 'structure': 2, 'level': 2, 'macro': 2}
        asset_data = {
            'symbol': 'TESTCOIN', 'latest_price': 100.0, 'consensio': {}, 'ema_ribbon': {},
            'funding_rate': -0.001,  # crowded shorts -> long +1 (favorable)
        }

        with _patched_engine(_fixed_criteria(scores, all_opposed=True)):
            result = score_asset(asset_data, macro_data, 'long')

        assert result['total'] == 5, result
        assert any('opposed' in v.lower() for v in result['vetoes'])
        assert any('crowded shorts' in m for m in result['modifiers'])


# ---------------------------------------------------------------------------
# Event-risk verdict cap. An imminent HIGH-impact macro event
# (fetched once per score_all_assets pass, passed into score_asset via the
# event_risk= kwarg) demotes a STRONG verdict to GOOD. Score is never
# touched — only the verdict label + a modifiers evidence entry. Direction-
# agnostic; lower verdicts are left alone.
# ---------------------------------------------------------------------------

class TestEventRiskVerdictCap:
    def test_strong_demoted_to_good_with_evidence(self):
        macro_data = _make_macro_data()
        # total = 2+2+2+2+2+1 = 11 -> STRONG baseline
        scores = {'trend': 2, 'momentum': 2, 'rsi': 2, 'structure': 2, 'level': 2, 'macro': 1}
        asset_data = {'symbol': 'TESTCOIN', 'latest_price': 100.0, 'consensio': {}, 'ema_ribbon': {}}
        event_risk = {'title': 'FOMC Rate Decision', 'hours_until': 14.0, 'impact': 'high'}

        with _patched_engine(_fixed_criteria(scores)):
            result = score_asset(asset_data, macro_data, 'long', event_risk=event_risk)

        assert result['total'] == 11, result  # score untouched
        assert result['verdict'] == 'GOOD', result
        assert any('FOMC Rate Decision in 14h' in m and 'verdict capped' in m
                   for m in result['modifiers']), result['modifiers']

    def test_strong_demoted_direction_agnostic_short(self):
        """The demotion is not long-only — SHORT gets it too."""
        macro_data = _make_macro_data()
        scores = {'trend': 2, 'momentum': 2, 'rsi': 2, 'structure': 2, 'level': 2, 'macro': 1}
        asset_data = {'symbol': 'TESTCOIN', 'latest_price': 100.0, 'consensio': {}, 'ema_ribbon': {}}
        event_risk = {'title': 'CPI Release', 'hours_until': 6.0, 'impact': 'high'}

        with _patched_engine(_fixed_criteria(scores)):
            result = score_asset(asset_data, macro_data, 'short', event_risk=event_risk)

        assert result['total'] == 11
        assert result['verdict'] == 'GOOD'
        assert any('verdict capped' in m for m in result['modifiers'])

    def test_good_verdict_unaffected(self):
        """A non-STRONG (GOOD) verdict is left alone even with an imminent event."""
        macro_data = _make_macro_data()
        # total = 2+2+2+1+1+0 = 8 -> GOOD baseline
        scores = {'trend': 2, 'momentum': 2, 'rsi': 2, 'structure': 1, 'level': 1, 'macro': 0}
        asset_data = {'symbol': 'TESTCOIN', 'latest_price': 100.0, 'consensio': {}, 'ema_ribbon': {}}
        event_risk = {'title': 'NFP Release', 'hours_until': 3.0, 'impact': 'high'}

        with _patched_engine(_fixed_criteria(scores)):
            result = score_asset(asset_data, macro_data, 'long', event_risk=event_risk)

        assert result['total'] == 8
        assert result['verdict'] == 'GOOD'
        assert result['modifiers'] == []

    def test_no_event_byte_identical_to_baseline(self):
        """event_risk=None (the default) must be identical to omitting the arg."""
        macro_data = _make_macro_data()
        scores = {'trend': 2, 'momentum': 2, 'rsi': 2, 'structure': 2, 'level': 2, 'macro': 1}
        asset_data = {'symbol': 'TESTCOIN', 'latest_price': 100.0, 'consensio': {}, 'ema_ribbon': {}}

        with _patched_engine(_fixed_criteria(scores)):
            baseline = score_asset(asset_data, macro_data, 'long')
        with _patched_engine(_fixed_criteria(scores)):
            explicit_none = score_asset(asset_data, macro_data, 'long', event_risk=None)

        assert baseline == explicit_none
        assert baseline['verdict'] == 'STRONG'
        assert baseline['modifiers'] == []

    def test_hard_veto_cap_unaffected_by_event_risk(self):
        """A hard-veto'd asset never reaches STRONG, so there's nothing to demote."""
        macro_data = _make_macro_data()
        scores = {'trend': 0, 'momentum': 2, 'rsi': 2, 'structure': 2, 'level': 2, 'macro': 2}
        asset_data = {'symbol': 'TESTCOIN', 'latest_price': 100.0, 'consensio': {}, 'ema_ribbon': {}}
        event_risk = {'title': 'FOMC Rate Decision', 'hours_until': 2.0, 'impact': 'high'}

        with _patched_engine(_fixed_criteria(scores, all_opposed=True)):
            result = score_asset(asset_data, macro_data, 'long', event_risk=event_risk)

        assert result['total'] == 5
        assert result['verdict'] != 'STRONG'
        assert not any('verdict capped' in m for m in result['modifiers'])


# ---------------------------------------------------------------------------
# score_all_assets integration — the event lookup happens ONCE
# per pass (not per asset), is attached to the payload's top level next to
# 'macro', and is applied as a verdict cap unless SCANNER_EVENT_RISK_CAP=0.
# ---------------------------------------------------------------------------

class TestEventRiskScoreAllAssetsIntegration:
    def _assets(self, n=3):
        return [
            {'symbol': f'COIN{i}', 'type': 'crypto', 'latest_price': 100.0 + i,
             'consensio': {}, 'ema_ribbon': {}}
            for i in range(n)
        ]

    def test_fetched_once_per_pass_not_per_asset(self, monkeypatch):
        monkeypatch.setenv('SCANNER_EVENT_RISK_CAP', '1')
        monkeypatch.setenv('SCANNER_EVENT_RISK_HOURS', '36')

        calls = {'n': 0}

        def fake_get_imminent_event(hours=36):
            calls['n'] += 1
            return {'title': 'FOMC Rate Decision', 'hours_until': 14.0, 'impact': 'high'}

        # STRONG baseline for every asset so the cap has something to do
        scores = {'trend': 2, 'momentum': 2, 'rsi': 2, 'structure': 2, 'level': 2, 'macro': 1}

        with patch('carrac.scanner.event_risk.get_imminent_event', fake_get_imminent_event), \
             _patched_engine(_fixed_criteria(scores)):
            payload = score_all_assets(self._assets(5), macro_data=_make_macro_data())

        assert calls['n'] == 1, "event lookup must happen once per pass, not per asset"
        assert payload['event_risk'] == {'title': 'FOMC Rate Decision', 'hours_until': 14.0}
        for r in payload['results']:
            assert r['long']['verdict'] == 'GOOD'
            assert r['short']['verdict'] == 'GOOD'
            assert r['best_verdict'] == 'GOOD'

    def test_no_event_payload_field_is_none_and_results_unchanged(self, monkeypatch):
        scores = {'trend': 2, 'momentum': 2, 'rsi': 2, 'structure': 2, 'level': 2, 'macro': 1}

        with patch('carrac.scanner.event_risk.get_imminent_event', return_value=None), \
             _patched_engine(_fixed_criteria(scores)):
            payload = score_all_assets(self._assets(2), macro_data=_make_macro_data())

        assert payload['event_risk'] is None
        for r in payload['results']:
            assert r['long']['verdict'] == 'STRONG'
            assert r['long']['modifiers'] == []
            assert r['short']['verdict'] == 'STRONG'
            assert r['short']['modifiers'] == []

    def test_cap_disabled_via_env_keeps_badge_but_not_demotion(self, monkeypatch):
        monkeypatch.setenv('SCANNER_EVENT_RISK_CAP', '0')

        scores = {'trend': 2, 'momentum': 2, 'rsi': 2, 'structure': 2, 'level': 2, 'macro': 1}

        with patch('carrac.scanner.event_risk.get_imminent_event',
                   return_value={'title': 'CPI Release', 'hours_until': 5.0, 'impact': 'high'}), \
             _patched_engine(_fixed_criteria(scores)):
            payload = score_all_assets(self._assets(2), macro_data=_make_macro_data())

        # Badge still attached (informational) — only the demotion is gated by CAP.
        assert payload['event_risk'] == {'title': 'CPI Release', 'hours_until': 5.0}
        for r in payload['results']:
            assert r['long']['verdict'] == 'STRONG'
            assert r['long']['modifiers'] == []
