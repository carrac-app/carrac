"""Tests for the onchain_get_smart_money MCP tool.

Covers:
- Tool is registered in TOOL_REGISTRY
- Tool requires premium tier
- Schema has asset as required field
- Returns expected keys when data is available (mocked DB)
- Raises ToolError when asset is missing from DB
- Raises ToolError when asset argument is empty
- disclaimer field is non-advice
- bias labels map correctly
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date
from unittest.mock import MagicMock, patch

import pytest


# Ensure tools are registered
import carrac.mcp.tools  # noqa: F401


# ─── Tool registration ─────────────────────────────────────────────────────────

class TestRegistration:
    @pytest.fixture(scope='class')
    @classmethod
    def registry(cls):
        from carrac.mcp.registry import TOOL_REGISTRY
        return TOOL_REGISTRY

    def test_tool_registered(self, registry):
        assert 'onchain_get_smart_money' in registry

    def test_tool_is_premium(self, registry):
        assert registry['onchain_get_smart_money']['min_tier'] == 'premium'

    def test_schema_has_required_asset(self, registry):
        schema = registry['onchain_get_smart_money']['inputSchema']
        assert 'asset' in schema['properties']
        assert 'asset' in schema.get('required', [])

    def test_description_mentions_hyperliquid(self, registry):
        desc = registry['onchain_get_smart_money']['description']
        assert 'Hyperliquid' in desc

    def test_description_mentions_disclaimer(self, registry):
        desc = registry['onchain_get_smart_money']['description']
        # The description must actually advertise the returned disclaimer
        # field — not just happen to omit the word "buy" (which proves nothing).
        assert 'disclaimer' in desc.lower()


# ─── onchain_get_smart_money behaviour ────────────────────────────────────

def _make_conn(value=None, ts_str='2026-06-30'):
    """Build a minimal mock DB connection that returns one row."""

    @contextmanager
    def _ctx():
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value = cur
        if value is not None:
            cur.fetchone.return_value = {
                'value': value,
                'ts': date.fromisoformat(ts_str),
            }
        else:
            cur.fetchone.return_value = None
        yield conn

    return _ctx


class TestGetSmartMoneyPositioning:
    def _call(self, arguments, value=None, ts_str='2026-06-30'):
        from carrac.mcp.tools.onchain import onchain_get_smart_money
        with patch('carrac.mcp.tools.onchain.get_conn' if False else
                   'carrac.db_pool.get_quant_conn', _make_conn(value, ts_str)):
            return onchain_get_smart_money(arguments)

    def _call_patched(self, arguments, row_value, ts_str='2026-06-30'):
        """Patch carrac.db_pool.get_quant_conn inside the tool function's import scope."""
        import carrac.db_pool as qdb
        with patch.object(qdb, 'get_quant_conn', _make_conn(row_value, ts_str)):
            from carrac.mcp.tools.onchain import onchain_get_smart_money
            return onchain_get_smart_money(arguments)

    def test_returns_required_keys(self):
        from carrac.mcp.tools.onchain import onchain_get_smart_money
        import carrac.db_pool as qdb
        with patch.object(qdb, 'get_quant_conn', _make_conn(68.4)):
            result = onchain_get_smart_money({'asset': 'BTC'})
        for key in ('asset', 'bias', 'label', 'data_date', 'summary', 'disclaimer'):
            assert key in result, f"Key {key!r} missing from result"

    def test_correct_asset_returned(self):
        from carrac.mcp.tools.onchain import onchain_get_smart_money
        import carrac.db_pool as qdb
        with patch.object(qdb, 'get_quant_conn', _make_conn(68.4)):
            result = onchain_get_smart_money({'asset': 'BTC'})
        assert result['asset'] == 'BTC'

    def test_bias_value_rounded(self):
        from carrac.mcp.tools.onchain import onchain_get_smart_money
        import carrac.db_pool as qdb
        with patch.object(qdb, 'get_quant_conn', _make_conn(68.456)):
            result = onchain_get_smart_money({'asset': 'BTC'})
        assert result['bias'] == pytest.approx(68.5, abs=0.01)

    def test_label_net_long(self):
        from carrac.mcp.tools.onchain import onchain_get_smart_money
        import carrac.db_pool as qdb
        with patch.object(qdb, 'get_quant_conn', _make_conn(70.0)):
            result = onchain_get_smart_money({'asset': 'BTC'})
        assert result['label'] == 'net long'

    def test_label_balanced(self):
        from carrac.mcp.tools.onchain import onchain_get_smart_money
        import carrac.db_pool as qdb
        with patch.object(qdb, 'get_quant_conn', _make_conn(50.0)):
            result = onchain_get_smart_money({'asset': 'ETH'})
        assert result['label'] == 'balanced'

    def test_label_net_short(self):
        from carrac.mcp.tools.onchain import onchain_get_smart_money
        import carrac.db_pool as qdb
        with patch.object(qdb, 'get_quant_conn', _make_conn(20.0)):
            result = onchain_get_smart_money({'asset': 'SOL'})
        assert result['label'] == 'net short'

    def test_data_date_populated(self):
        from carrac.mcp.tools.onchain import onchain_get_smart_money
        import carrac.db_pool as qdb
        with patch.object(qdb, 'get_quant_conn', _make_conn(68.0, '2026-06-30')):
            result = onchain_get_smart_money({'asset': 'BTC'})
        assert '2026-06-30' in result['data_date']

    def test_summary_contains_asset_and_label(self):
        from carrac.mcp.tools.onchain import onchain_get_smart_money
        import carrac.db_pool as qdb
        with patch.object(qdb, 'get_quant_conn', _make_conn(70.0)):
            result = onchain_get_smart_money({'asset': 'BTC'})
        assert 'BTC' in result['summary']
        assert 'net long' in result['summary']

    def test_disclaimer_is_non_advice(self):
        from carrac.mcp.tools.onchain import onchain_get_smart_money
        import carrac.db_pool as qdb
        with patch.object(qdb, 'get_quant_conn', _make_conn(68.0)):
            result = onchain_get_smart_money({'asset': 'BTC'})
        disc = result['disclaimer'].lower()
        assert 'not' in disc
        assert 'advice' in disc
        # Must not contain buy/sell as affirmative guidance — assert the
        # actual phrase (was a near-tautological "buy" not in disc or "not"
        # in disc, which 'not' would match on words like "notice"/"cannot").
        assert 'not a signal to buy' in disc

    def test_raises_tool_error_when_no_data(self):
        from carrac.mcp.tools.onchain import onchain_get_smart_money
        from carrac.mcp.protocol import ToolError
        import carrac.db_pool as qdb
        with patch.object(qdb, 'get_quant_conn', _make_conn(None)):
            with pytest.raises(ToolError, match='No smart-money positioning data'):
                onchain_get_smart_money({'asset': 'UNKNOWN'})

    def test_raises_tool_error_when_asset_empty(self):
        from carrac.mcp.tools.onchain import onchain_get_smart_money
        from carrac.mcp.protocol import ToolError
        import carrac.db_pool as qdb
        with patch.object(qdb, 'get_quant_conn', _make_conn(None)):
            with pytest.raises(ToolError, match='asset is required'):
                onchain_get_smart_money({'asset': ''})

    def test_asset_is_uppercased(self):
        """lowercase asset should be uppercased transparently."""
        from carrac.mcp.tools.onchain import onchain_get_smart_money
        import carrac.db_pool as qdb
        with patch.object(qdb, 'get_quant_conn', _make_conn(60.0)):
            result = onchain_get_smart_money({'asset': 'btc'})
        assert result['asset'] == 'BTC'


# ─── on_chain module: _bias_label helper ──────────────────────────────────────

class TestOnchainBiasLabel:
    def test_label_boundaries(self):
        from carrac.mcp.tools.onchain import _bias_label
        assert _bias_label(65.0) == 'net long'
        assert _bias_label(64.9) == 'mildly net long'
        assert _bias_label(55.0) == 'mildly net long'
        assert _bias_label(54.9) == 'balanced'
        assert _bias_label(45.0) == 'balanced'
        assert _bias_label(44.9) == 'mildly net short'
        assert _bias_label(35.0) == 'mildly net short'
        assert _bias_label(34.9) == 'net short'
