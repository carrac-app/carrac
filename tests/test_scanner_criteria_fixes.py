"""Tests for scanner-criteria string matching.

Both bugs were string comparisons against the wrong producer values, so a
scoring branch silently never fired. These lock the branches ON.
"""
from carrac.scanner.scorer.criteria import _score_momentum, _score_level


# --- Bug A: momentum EMA-ribbon leg (producer emits 'bull'/'bear', not 'bullish') ---

def test_momentum_ribbon_leg_fires_long():
    # 'bull' ribbon + positive MACD → both confirm → 2 (was 1 when the leg was dead).
    asset = {'ema_ribbon': {'trend': 'bull'}, 'macd': {'histogram': 1.5}}
    score, desc, _ = _score_momentum(asset, 'long')
    assert score == 2
    assert 'EMAs bullish' in desc


def test_momentum_ribbon_leg_fires_short():
    asset = {'ema_ribbon': {'trend': 'bear'}, 'macd': {'histogram': -1.5}}
    score, desc, _ = _score_momentum(asset, 'short')
    assert score == 2
    assert 'EMAs bearish' in desc


def test_momentum_ribbon_neutral_scores_macd_only():
    asset = {'ema_ribbon': {'trend': 'neutral'}, 'macd': {'histogram': 1.0}}
    score, _, _ = _score_momentum(asset, 'long')
    assert score == 1  # only MACD confirms; ribbon neutral contributes nothing


def test_momentum_ribbon_opposes_scores_macd_only():
    # 'bear' ribbon on a long trade must NOT confirm.
    asset = {'ema_ribbon': {'trend': 'bear'}, 'macd': {'histogram': 1.0}}
    score, _, _ = _score_momentum(asset, 'long')
    assert score == 1


# --- Bug B: level extension penalty (producer emits Title-Case 'Below S3'/'Above R3') ---

def test_level_extension_penalty_fires_on_titlecase():
    # One favorable nearby resistance → base score 1; at 'Below S3' the short
    # extension penalty must drop it to 0 (was left at 1 when the case mismatched).
    extreme = {'latest_price': 100.0, 'cpr': {'price_level': 'Below S3', 'r1': 101.5}, 'bb': {}}
    normal = {'latest_price': 100.0, 'cpr': {'price_level': 'S1', 'r1': 101.5}, 'bb': {}}
    assert _score_level(normal, 'short')[0] == 1        # no penalty at a non-extreme level
    assert _score_level(extreme, 'short')[0] == 0        # penalty fired
