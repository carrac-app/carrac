"""Tests for MCP trade_log persisting account_tag + size.

Context: the trade_journal table has `account_tag` and `size`
columns (used by the Flask + FastAPI journal write paths via the
`db.trade_update(entry_id, **extended)` pattern), so the MCP `trade_log`
tool must persist both: `account_tag` belongs in the tool's inputSchema,
and `size` must be passed through to the DB write.

Covers:
- trade_log with account_tag + size persists both to the DB row
- trade_log without account_tag/size leaves both null
- inputSchema advertises account_tag as an optional string
- malformed (non-numeric) size raises a clean ToolError BEFORE the trade
  row is committed — no orphaned row, no -32603, no missing trade_id for
  a retrying caller
"""

import pytest

import carrac.db_pool as _cdb

from carrac.mcp import tools  # noqa: F401 — registers all tools
from carrac.mcp.protocol import ToolError
from carrac.mcp.registry import TOOL_REGISTRY


@pytest.fixture(autouse=True)
def _bind_user(bind_mcp_user):
    """Bind user_id=42 for all tests in this module."""
    return bind_mcp_user


def _make_db(tmp_path, monkeypatch, db_name='log_trade_extended.db'):
    _cdb._db_type = None
    monkeypatch.setenv('DB_TYPE', 'sqlite')
    from carrac import content_db as cdb_mod
    from carrac.content_db import ContentDatabase
    test_db = ContentDatabase(db_path=str(tmp_path / db_name))
    monkeypatch.setattr(cdb_mod, 'get_content_db', lambda: test_db)
    return test_db


class TestLogTradeInputSchema:
    def test_account_tag_in_schema(self):
        schema = TOOL_REGISTRY['trade_log']['inputSchema']
        assert 'account_tag' in schema['properties']
        assert schema['properties']['account_tag']['type'] == 'string'


class TestLogTradeAccountTagAndSize:
    def test_log_trade_persists_account_tag_and_size(self, tmp_path, monkeypatch):
        test_db = _make_db(tmp_path, monkeypatch, 'with_extended.db')

        fn = TOOL_REGISTRY['trade_log']['handler']
        result = fn({
            'asset': 'BTC', 'entry_price': 65000,
            'account_tag': 'main', 'size': 2.5,
        })
        tid = result['trade_id']
        entry = test_db.get_journal_entry(tid)
        assert entry['account_tag'] == 'main'
        assert entry['size'] == 2.5

    def test_log_trade_without_account_tag_or_size_is_null(self, tmp_path, monkeypatch):
        test_db = _make_db(tmp_path, monkeypatch, 'without_extended.db')

        fn = TOOL_REGISTRY['trade_log']['handler']
        result = fn({'asset': 'ETH', 'entry_price': 3000})
        tid = result['trade_id']
        entry = test_db.get_journal_entry(tid)
        assert entry['account_tag'] is None
        assert entry['size'] is None


class TestLogTradeMalformedSizeNoOrphanRow:
    def test_non_numeric_size_raises_tool_error(self, tmp_path, monkeypatch):
        """A non-numeric size must fail BEFORE the DB
        write, as a clean ToolError — not a post-commit TypeError that the
        MCP dispatcher turns into a -32603 Internal error while a trade row
        was already durably written (which would silently orphan a row and
        cause a retrying caller to create a duplicate)."""
        _make_db(tmp_path, monkeypatch, 'malformed_size.db')

        fn = TOOL_REGISTRY['trade_log']['handler']
        with pytest.raises(ToolError, match='size must be a valid number'):
            fn({'asset': 'BTC', 'entry_price': 65000, 'size': 'not-a-number'})

    def test_non_numeric_size_writes_no_row(self, tmp_path, monkeypatch):
        test_db = _make_db(tmp_path, monkeypatch, 'malformed_size_no_row.db')

        fn = TOOL_REGISTRY['trade_log']['handler']
        with pytest.raises(ToolError):
            fn({'asset': 'BTC', 'entry_price': 65000, 'size': 'not-a-number'})

        trades = test_db.get_trade_history(42, days=3650, limit=100)
        assert trades == []

    def test_non_positive_size_silently_dropped_not_rejected(self, tmp_path, monkeypatch):
        """Control: a parseable but non-positive size keeps the existing
        silent-drop behaviour (mirrors the Flask/FastAPI `size > 0` gate) —
        only genuinely unparseable input should raise."""
        test_db = _make_db(tmp_path, monkeypatch, 'zero_size.db')

        fn = TOOL_REGISTRY['trade_log']['handler']
        result = fn({'asset': 'BTC', 'entry_price': 65000, 'size': 0})
        entry = test_db.get_journal_entry(result['trade_id'])
        assert entry['size'] is None
