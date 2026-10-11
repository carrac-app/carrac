"""Confluence engine: per-level weights, diversity bonus, single-linkage
clustering, and the enriched (display) zone shape.

Backward-compat invariant: `label` stays a CODE join (e.g. 'POC+SMA200')
because carrac/scanner/scorer/plans.py parses it by code substring; friendly
names live in the new `label_friendly` field.
"""

import carrac.scanner.confluence as cf
from carrac.scanner.labels import (
    LEVEL_WEIGHTS, LABEL_NAMES, LEVEL_FAMILY,
)


# ── labels.py tables ──────────────────────────────────────────────

def test_label_tables_cover_every_engine_label():
    engine_labels = {
        'PIVOT', 'TC', 'BC', 'S1', 'S2', 'S3', 'R1', 'R2', 'R3',
        'BB_U', 'BB_M', 'BB_L', 'EMA30', 'EMA60', 'EMA21', 'EMA50', 'EMA200', 'SMA200',
        'SW_H', 'SW_PH', 'SW_L', 'SW_PL', 'POC', 'VAH', 'VAL',
        'PDH', 'PDL', 'MH', 'ML', 'PWH', 'PWL', 'PMH', 'PML',
    }
    for lbl in engine_labels:
        assert lbl in LEVEL_WEIGHTS, f"{lbl} missing weight"
        assert lbl in LABEL_NAMES, f"{lbl} missing friendly name"
        assert lbl in LEVEL_FAMILY, f"{lbl} missing family"


def test_top_tier_weights():
    assert LEVEL_WEIGHTS['POC'] == 3.0
    assert LEVEL_WEIGHTS['SMA200'] == 3.0
    assert LEVEL_WEIGHTS['EMA200'] == 3.0
    assert LEVEL_WEIGHTS['PIVOT'] == 3.0      # monthly CPR pivot
    assert LEVEL_WEIGHTS['EMA21'] == 2.0
    assert LEVEL_WEIGHTS['PDH'] == 1.0        # daily, supporting


# ── clustering ────────────────────────────────────────────────────

def _levels_asset(prices):
    """Minimal asset_data: distinct key_levels so each price gets a label."""
    keymap = ['pdh', 'pdl', 'mh', 'ml', 'pwh', 'pwl', 'pmh', 'pml']
    return {'key_levels': {keymap[i]: p for i, p in enumerate(prices)}}


def test_single_linkage_chains_near_pairs():
    # 100, 100.4, 100.8 (each step 0.4% < 0.5% CLUSTER_PCT) chain into ONE zone.
    out = cf.compute_confluences(_levels_asset([100.0, 100.4, 100.8]), price=120.0, direction='long')
    supports = out['supports']
    assert len(supports) == 1
    assert 100.3 < supports[0]['price'] < 100.5     # simple mean ≈ 100.4


def test_span_cap_splits_runaway_chain():
    prices = [100.0, 100.4, 100.8, 101.2, 101.6, 102.0]
    out = cf.compute_confluences(_levels_asset(prices), price=120.0, direction='long')
    assert len(out['supports']) >= 2                # MAX_ZONE_PCT prevents one giant band


# ── enriched zone shape ───────────────────────────────────────────

def test_zone_shape_codes_friendly_members_tier_span():
    out = cf.compute_confluences(
        {'vp_levels': {'poc': 100.0}, 'ma_levels': {'sma200': 100.2}},
        price=120.0, direction='long',
    )
    z = out['supports'][0]
    # label stays CODE-join for the scorer's substring parser
    assert z['label'] == 'POC+SMA200'
    # friendly display label is separate
    assert z['label_friendly'] == 'Point of Control + SMA 200'
    assert z['tier'] in ('strong', 'medium', 'faint')
    assert {m['code'] for m in z['members']} == {'POC', 'SMA200'}
    assert all({'code', 'name', 'price', 'weight', 'family'} <= set(m) for m in z['members'])
    assert z['span'][0] <= z['price'] <= z['span'][1]


def test_scorer_facing_fields_unchanged():
    # plans.py / MCP / routes read these — keep them present.
    out = cf.compute_confluences({'vp_levels': {'poc': 100.0}}, price=120.0, direction='long')
    z = out['supports'][0]
    for k in ('price', 'label', 'sources', 'strength', 'side', 'count'):
        assert k in z
    assert z['side'] == 'support'
    assert 'vp' in z['sources']


def test_diversity_bonus_beats_same_family_at_equal_base_weight():
    # Both pairs sum to base weight 6.0; the diverse one (vp+ma) must score higher
    # than the same-family one (ma+ma) thanks to the diversity multiplier.
    diverse = cf.compute_confluences(
        {'vp_levels': {'poc': 100.0}, 'ma_levels': {'sma200': 100.1}},
        price=120.0, direction='long',
    )['supports'][0]['strength']
    same = cf.compute_confluences(
        {'ma_levels': {'sma200': 100.0, 'ema200': 100.1}},
        price=120.0, direction='long',
    )['supports'][0]['strength']
    assert diverse > same


def test_empty_input_safe():
    out = cf.compute_confluences({}, price=0, direction='long')
    assert out == {'supports': [], 'resistances': [], 'all': []}
