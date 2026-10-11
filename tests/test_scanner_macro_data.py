"""Pins the scanner macro-block behaviour.

Behaviour: carrac.scanner.scorer.engine._build_macro_data() calls
carrac.sentiment.regime.get_or_compute_regime(), the shared
get-or-compute-and-cache helper that the market_get_sentiment MCP tool and
/api/market-sentiment also use, so a cold regime cache still yields real
values. A genuine total failure (cache miss AND a failing fresh compute)
produces an EXPLICIT `available: False` / `regime: 'Unavailable'` sentinel
instead of neutral-looking defaults (fear_greed=50 is the neutral midpoint and
would be indistinguishable from a real reading), and is logged.

Covers:
  - TestBuildMacroDataHealthyPath: warm cache -> correct real values.
  - TestBuildMacroDataColdCache: cold cache but the underlying regime
    compute succeeds -> real values, not neutral-looking defaults.
  - TestBuildMacroDataDegradedPath: cold cache AND compute fails -> explicit
    'Unavailable' sentinel, warning logged, never fabricated neutral values.
  - TestScoreMacroDistinguishesUnavailableFromNeutral: the unavailable
    sentinel scores differently in evidence text (never claims "Neutral"/a
    numeric F&G) from a genuine neutral reading, even though both currently
    score 0/1 on the 0-2 scale.
  - TestGetOrComputeRegime: the shared helper's three paths in
    isolation (cache hit / cache miss+compute+cache / total failure).
"""
from __future__ import annotations

import logging
from unittest.mock import patch

from carrac.scanner.scorer.criteria import _score_macro
from carrac.scanner.scorer.engine import _build_macro_data
from carrac.sentiment.regime import get_or_compute_regime


_REAL_REGIME = {
    'regime': 'Neutral',
    'score': 2,
    'color': '#d29922',
    'components': [
        {'name': 'VIX', 'score': 1, 'detail': '18.6'},
        {'name': 'F&G', 'score': 0, 'detail': '26'},
        {'name': 'Breadth', 'score': -1, 'detail': '24%'},
    ],
    'computed_at': '2026-07-26T13:28:54',
}


# ---------------------------------------------------------------------------
# _build_macro_data — healthy path (cache already warm)
# ---------------------------------------------------------------------------

class TestBuildMacroDataHealthyPath:
    def test_warm_cache_produces_real_values(self):
        with patch('carrac.sentiment.regime.get_or_compute_regime', return_value=_REAL_REGIME):
            macro = _build_macro_data()

        assert macro['regime'] == 'Neutral'
        assert macro['regime_score'] == 2
        assert macro['fear_greed'] == 26
        assert macro['breadth'] == {'pct': 24.0}
        assert macro['available'] is True


# ---------------------------------------------------------------------------
# _build_macro_data — exact prod-bug reproduction: regime cache is cold at
# the moment the scanner precompute pass runs, but a fresh compute of the
# regime succeeds (as it always did — market_get_sentiment always could compute
# it). This is the scenario that must not silently return the placeholder
# defaults; it must return the real computed values.
# ---------------------------------------------------------------------------

class TestBuildMacroDataColdCache:
    def test_cold_cache_falls_through_to_fresh_compute_not_defaults(self):
        with patch('carrac.caches.get_cached_regime', return_value=None), \
             patch('carrac.caches.set_cached_regime') as mock_set, \
             patch('carrac.sentiment.regime.gather_regime_inputs', return_value={'fear_greed': 26}), \
             patch('carrac.sentiment.regime.compute_market_regime', return_value=_REAL_REGIME):
            macro = _build_macro_data()

        # Not the neutral-looking defaults (regime='Unknown', fear_greed=50,
        # regime_score=0, breadth={}), which are indistinguishable from real neutral.
        assert macro['regime'] == 'Neutral'
        assert macro['regime_score'] == 2
        assert macro['fear_greed'] == 26
        assert macro['available'] is True
        # The freshly-computed regime must also be cached so the NEXT
        # reader (another scanner pass, market_get_sentiment, etc.) gets a warm hit.
        mock_set.assert_called_once_with(_REAL_REGIME)


# ---------------------------------------------------------------------------
# _build_macro_data — genuine total failure: cache miss AND the fresh
# compute also fails (e.g. every upstream data source is down). Must be
# visibly marked unavailable, not silently defaulted, and must be logged.
# ---------------------------------------------------------------------------

class TestBuildMacroDataDegradedPath:
    def test_total_failure_marks_unavailable_not_neutral(self, caplog):
        with patch('carrac.caches.get_cached_regime', return_value=None), \
             patch('carrac.sentiment.regime.gather_regime_inputs', side_effect=RuntimeError('boom')), \
             caplog.at_level(logging.WARNING):
            macro = _build_macro_data()

        assert macro['available'] is False
        assert macro['regime'] == 'Unavailable'
        assert macro['regime_score'] == 0
        assert macro['fear_greed'] is None
        assert macro['breadth'] == {}
        # 'Unavailable' must never collide with a real regime label, and
        # fear_greed must never collide with the real neutral midpoint (50).
        assert macro['regime'] not in ('Unknown', 'Neutral', 'Risk-On', 'Cautious', 'Risk-Off', 'Crisis')
        assert macro['fear_greed'] != 50

        assert any('unavailable' in r.message.lower() for r in caplog.records), (
            "A total macro-data failure must be logged, not silent."
        )

    def test_empty_dict_regime_also_treated_as_unavailable(self):
        """get_or_compute_regime() returning a falsy-but-not-None value
        (defensive: e.g. {}) must be treated the same as None."""
        with patch('carrac.sentiment.regime.get_or_compute_regime', return_value={}):
            macro = _build_macro_data()

        assert macro['available'] is False
        assert macro['regime'] == 'Unavailable'


# ---------------------------------------------------------------------------
# _score_macro — the unavailable sentinel must never be scored/evidenced as
# if it were a genuine neutral reading.
# ---------------------------------------------------------------------------

class TestScoreMacroDistinguishesUnavailableFromNeutral:
    def test_unavailable_scores_zero_with_distinguishing_evidence(self):
        unavailable = {
            'regime': 'Unavailable', 'regime_score': 0,
            'fear_greed': None, 'breadth': {}, 'available': False,
        }

        for direction in ('long', 'short'):
            score, evidence, veto_info = _score_macro(unavailable, direction)
            assert score == 0
            assert 'unavailable' in evidence.lower()
            assert 'Neutral' not in evidence
            assert '50' not in evidence
            assert veto_info == {'hard_zero_long': False, 'hard_zero_short': False}

    def test_genuine_neutral_scores_differently_and_is_labelled_neutral(self):
        """A REAL neutral reading (available=True, regime='Neutral') must
        still score 1 (per the existing Neutral rule) and its evidence must
        say 'Neutral' — proving the two states are distinguishable both in
        score semantics available-flag and in the evidence text."""
        genuine_neutral = {
            'regime': 'Neutral', 'regime_score': 2,
            'fear_greed': 50, 'breadth': {}, 'available': True,
        }

        for direction in ('long', 'short'):
            score, evidence, veto_info = _score_macro(genuine_neutral, direction)
            assert score == 1
            assert 'Neutral' in evidence
            assert 'unavailable' not in evidence.lower()

    def test_missing_available_key_defaults_to_scored_legacy_behaviour(self):
        """Hand-built macro dicts without an 'available' key (existing
        tests, older callers) must keep scoring normally — only an EXPLICIT
        available=False should take the unavailable branch."""
        legacy_dict = {'regime': 'Neutral', 'regime_score': 5, 'fear_greed': 50, 'breadth': {}}
        score, evidence, veto_info = _score_macro(legacy_dict, 'long')
        assert score != 0 or 'unavailable' not in evidence.lower()
        assert 'unavailable' not in evidence.lower()


# ---------------------------------------------------------------------------
# get_or_compute_regime — the extracted shared helper, in isolation.
# ---------------------------------------------------------------------------

class TestGetOrComputeRegime:
    def test_cache_hit_returns_cached_value_without_recomputing(self):
        with patch('carrac.caches.get_cached_regime', return_value=_REAL_REGIME), \
             patch('carrac.sentiment.regime.gather_regime_inputs') as mock_gather, \
             patch('carrac.sentiment.regime.compute_market_regime') as mock_compute:
            result = get_or_compute_regime()

        assert result == _REAL_REGIME
        mock_gather.assert_not_called()
        mock_compute.assert_not_called()

    def test_cache_miss_computes_fresh_and_caches_it(self):
        with patch('carrac.caches.get_cached_regime', return_value=None), \
             patch('carrac.caches.set_cached_regime') as mock_set, \
             patch('carrac.sentiment.regime.gather_regime_inputs', return_value={'fear_greed': 26}), \
             patch('carrac.sentiment.regime.compute_market_regime', return_value=_REAL_REGIME) as mock_compute:
            result = get_or_compute_regime()

        assert result == _REAL_REGIME
        mock_compute.assert_called_once_with(fear_greed=26)
        mock_set.assert_called_once_with(_REAL_REGIME)

    def test_total_failure_returns_none_never_raises(self, caplog):
        with patch('carrac.caches.get_cached_regime', return_value=None), \
             patch('carrac.sentiment.regime.gather_regime_inputs', side_effect=RuntimeError('upstream down')), \
             caplog.at_level(logging.WARNING):
            result = get_or_compute_regime()

        assert result is None
