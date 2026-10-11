"""MCP scanner_get_confluence_zones — enriched zone shape + error paths.

Verifies that the refactored tool:
- delegates to get_symbol_confluence (via the shared helper)
- returns label_friendly, tier, and members on each zone
- correctly counts total_zones
- raises ToolError on cold cache, unknown symbol, and blank symbol
"""

import pytest

import carrac.caches as caches_mod
from carrac.mcp import tools  # noqa: F401  registration side-effect
from carrac.mcp.tools.scanner import scanner_get_confluence_zones
from carrac.mcp.protocol import ToolError
from carrac.mcp.runtime import set_mcp_caller, reset_mcp_caller


# ---------------------------------------------------------------------------
# Fake scanner cache payload
# ---------------------------------------------------------------------------

def _make_zone(price, side, label='POC+SMA200', label_friendly='Point of Control + SMA 200',
               strength=8, tier='strong'):
    return {
        'price': price,
        'label': label,
        'label_friendly': label_friendly,
        'sources': ['POC', 'SMA200'],
        'members': [
            {'code': 'POC', 'name': 'Point of Control', 'price': price, 'weight': 5, 'family': 'vp'},
            {'code': 'SMA200', 'name': 'SMA 200', 'price': price, 'weight': 3, 'family': 'ma'},
        ],
        'strength': strength,
        'tier': tier,
        'side': side,
        'span': [price * 0.999, price * 1.001],
        'count': 2,
    }


SUPPORT_ZONE = _make_zone(90, 'support', strength=9, tier='strong')
RESISTANCE_ZONE = _make_zone(110, 'resistance', label='EMA50', label_friendly='EMA 50',
                              strength=6, tier='medium')

FAKE_SCANNER = {
    'results': [
        {
            'symbol': 'BTC',
            'price': 100,
            'long': {
                'confluences': {
                    'all': [SUPPORT_ZONE],
                }
            },
            'short': {
                'confluences': {
                    'all': [RESISTANCE_ZONE],
                }
            },
        }
    ],
    'macro': {},
    'computed_at': '2026-06-13T00:00:00Z',
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _setup_caller(monkeypatch):
    """Patch cache + set member tier; reset after each test."""
    monkeypatch.setattr(caches_mod, 'get_cached_scanner', lambda: FAKE_SCANNER)
    set_mcp_caller(user_id=1, tier='member')
    yield
    reset_mcp_caller()


# ---------------------------------------------------------------------------
# Happy-path
# ---------------------------------------------------------------------------

def test_returns_enriched_support_zone():
    out = scanner_get_confluence_zones({'symbol': 'BTC'})
    assert out['supports'], 'should have at least one support zone'
    sup = out['supports'][0]
    assert sup['label_friendly'] == 'Point of Control + SMA 200'
    assert sup['tier'] == 'strong'
    assert isinstance(sup['members'], list) and len(sup['members']) == 2


def test_returns_enriched_resistance_zone():
    out = scanner_get_confluence_zones({'symbol': 'BTC'})
    assert out['resistances'], 'should have at least one resistance zone'
    res = out['resistances'][0]
    assert res['label_friendly'] == 'EMA 50'
    assert res['tier'] == 'medium'
    assert isinstance(res['members'], list)


def test_total_zones_correct():
    out = scanner_get_confluence_zones({'symbol': 'BTC'})
    # one support + one resistance = 2 deduped zones in 'all'
    assert out['total_zones'] == 2


def test_symbol_normalised_to_upper():
    out = scanner_get_confluence_zones({'symbol': 'btc'})
    assert out['symbol'] == 'BTC'


def test_top_level_price_present():
    out = scanner_get_confluence_zones({'symbol': 'BTC'})
    assert out['price'] == 100


def test_zone_keys_present():
    out = scanner_get_confluence_zones({'symbol': 'BTC'})
    expected_keys = {'price', 'label', 'label_friendly', 'sources', 'members', 'strength', 'tier', 'side'}
    for zone in out['supports'] + out['resistances']:
        assert expected_keys.issubset(zone.keys()), f'missing keys in zone: {zone}'


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------

def test_cold_cache_raises(monkeypatch):
    monkeypatch.setattr(caches_mod, 'get_cached_scanner', lambda: None)
    with pytest.raises(ToolError, match='Scanner data not available'):
        scanner_get_confluence_zones({'symbol': 'BTC'})


def test_unknown_symbol_raises():
    with pytest.raises(ToolError, match='ETH not found in scanner data'):
        scanner_get_confluence_zones({'symbol': 'ETH'})


def test_blank_symbol_raises():
    with pytest.raises(ToolError, match='symbol is required'):
        scanner_get_confluence_zones({'symbol': ''})


def test_missing_symbol_key_raises():
    with pytest.raises(ToolError, match='symbol is required'):
        scanner_get_confluence_zones({})
