"""MCP scanner_get_screen free-tier cap — mirrors /api/scanner HTTP behavior.

Pins the gate: a free API key must not pull the
full untruncated scanner universe via the MCP scanner_get_screen tool while the
HTTP endpoint caps free callers at the top FREE_SCANNER_RESULT_LIMIT.
"""

import pytest

from carrac.mcp import tools  # noqa: F401  registration side-effect
from carrac.mcp.tools.scanner import scanner_get_screen, FREE_SCANNER_RESULT_LIMIT
from carrac.mcp.runtime import set_mcp_caller, reset_mcp_caller


def _fake_results(n):
    return [
        {
            'symbol': f'SYM{i}',
            'price': 100 + i,
            'best_verdict': 'GOOD',
            'long': {'total': 8, 'verdict': 'GOOD'},
            'short': {'total': 2, 'verdict': 'AVOID'},
        }
        for i in range(n)
    ]


FAKE_DATA = {
    'results': _fake_results(8),  # more than the free cap
    'macro': {},
    'computed_at': '2026-06-13T00:00:00Z',
}


@pytest.fixture(autouse=True)
def _patch_cache(monkeypatch):
    import carrac.caches as caches_mod
    monkeypatch.setattr(caches_mod, 'get_cached_scanner', lambda: FAKE_DATA)
    yield
    reset_mcp_caller()


def test_free_tier_capped_to_limit():
    set_mcp_caller(user_id=1, tier='free')
    out = scanner_get_screen({})
    assert len(out['results']) == FREE_SCANNER_RESULT_LIMIT


def test_member_tier_gets_full_universe():
    set_mcp_caller(user_id=2, tier='member')
    out = scanner_get_screen({})
    assert len(out['results']) == 8


def test_premium_tier_gets_full_universe():
    set_mcp_caller(user_id=3, tier='premium')
    out = scanner_get_screen({})
    assert len(out['results']) == 8
