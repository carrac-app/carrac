"""Malformed `limit` values must not pass silently.

registry.call_tool does not validate `arguments` against `input_schema` —
every enum/minimum/maximum in a tool schema is advisory unless the handler
re-checks it (see quant_get_data's window check, the model for this family).

market_get_overview(limit="twenty") must not fall through a bare
`except (TypeError, ValueError): limit = 20` and silently serve the default
with no error and no note — the caller could not tell their argument was
discarded. scanner_get_screen already rejects a non-integer limit and clamps
an out-of-range one to [1, MAX_SCANNER_LIMIT]; these tests lock that
behaviour in and give market_get_overview the same shape, matching how the
honest sibling quant_get_anomalies clamps `top`.
"""

import pytest

from carrac.mcp import tools  # noqa: F401  registration side-effect
from carrac.mcp.protocol import ToolError
from carrac.mcp.tools.market import market_get_overview
from carrac.mcp.tools.scanner import scanner_get_screen, MAX_SCANNER_LIMIT, DEFAULT_SCANNER_LIMIT
from carrac.mcp.runtime import set_mcp_caller, reset_mcp_caller


def _fake_results(n):
    return [
        {
            'symbol': f'SYM{i}',
            'price': 100 + i,
            'long': {'total': 8},
            'short': {'total': 2},
        }
        for i in range(n)
    ]


# 150 rows — bigger than both tools' max clamp (100) so an over-max limit is
# distinguishable from "there just weren't enough rows".
FAKE_DATA = {
    'results': _fake_results(150),
    'macro': {},
    'computed_at': '2026-08-11T00:00:00Z',
}


@pytest.fixture(autouse=True)
def _patch_cache(monkeypatch):
    import carrac.caches as caches_mod
    # Patch the dependency the tools call (get_cached_scanner), never the
    # tool functions under test themselves.
    monkeypatch.setattr(caches_mod, 'get_cached_scanner', lambda: dict(FAKE_DATA))
    set_mcp_caller(user_id=1, tier='member')
    yield
    reset_mcp_caller()


# ─── market_get_overview ────────────────────────────────────────────────

class TestMarketOverviewLimitValidation:
    def test_non_integer_string_limit_raises(self):
        with pytest.raises(ToolError) as exc:
            market_get_overview({'limit': 'twenty'})
        assert 'integer' in str(exc.value).lower()

    def test_non_integer_limit_does_not_silently_default(self):
        """An invalid limit must raise, not silently return 20 rows
        (the default)."""
        with pytest.raises(ToolError):
            market_get_overview({'limit': 'twenty'})

    def test_none_type_limit_raises(self):
        with pytest.raises(ToolError):
            market_get_overview({'limit': [1, 2, 3]})

    def test_negative_limit_is_clamped_to_one(self):
        out = market_get_overview({'limit': -5})
        assert len(out['results']) == 1
        assert out['result_count'] == 1

    def test_zero_limit_is_clamped_to_one(self):
        out = market_get_overview({'limit': 0})
        assert len(out['results']) == 1

    def test_over_max_limit_is_clamped_to_100(self):
        out = market_get_overview({'limit': 9999})
        assert len(out['results']) == 100

    def test_omitted_limit_uses_default_20(self):
        out = market_get_overview({})
        assert len(out['results']) == 20

    def test_valid_integer_limit_is_honored(self):
        out = market_get_overview({'limit': 7})
        assert len(out['results']) == 7

    def test_numeric_string_limit_is_accepted(self):
        """A well-formed numeric string ("7") is a legitimate integer, unlike
        "twenty" — int("7") succeeds, so this must not raise."""
        out = market_get_overview({'limit': '7'})
        assert len(out['results']) == 7


# ─── scanner_get_screen ─────────────────────────────────────────────────

class TestScannerScreenLimitValidation:
    """scanner_get_screen already validates type and clamps range
    (carrac/mcp/tools/scanner.py::scanner_get_screen) — these tests pin exactly
    the two scenarios: a non-integer limit and a negative limit."""

    def test_non_integer_string_limit_raises(self):
        with pytest.raises(ToolError) as exc:
            scanner_get_screen({'limit': 'abc'})
        assert 'integer' in str(exc.value).lower()

    def test_negative_limit_is_clamped_to_one(self):
        """scanner_get_screen(limit=-5)."""
        out = scanner_get_screen({'limit': -5})
        assert len(out['results']) == 1

    def test_over_max_limit_is_clamped(self):
        out = scanner_get_screen({'limit': 9999})
        assert len(out['results']) == MAX_SCANNER_LIMIT

    def test_omitted_limit_uses_default(self):
        out = scanner_get_screen({})
        assert len(out['results']) == DEFAULT_SCANNER_LIMIT
