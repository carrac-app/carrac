"""Tests for feat/trade-origin-tags — tags parameter on trade_log and trade_update MCP tools.

Covers:
- trade_log with tags persists to DB
- trade_log without tags leaves tags null
- trade_update sets tags on an existing trade
- trade_update clears tags with an empty list
- _validate_tags rejects non-list, oversized list, overlong tag, non-string item
- DB-layer log_trade_open accepts tags kwarg
"""

import json
import pytest
import carrac.db_pool as _cdb

from carrac.mcp import tools  # noqa: F401 — registers all tools
from carrac.mcp.registry import TOOL_REGISTRY
from carrac.mcp.protocol import ToolError
from carrac.mcp.tools.journal import _validate_tags


# ---------------------------------------------------------------------------
# Shared DB fixture (SQLite in-memory via tmp_path, same pattern as test_journal)
# ---------------------------------------------------------------------------

@pytest.fixture
def db(tmp_path, monkeypatch):
    """ContentDatabase backed by a fresh SQLite file."""
    _cdb._db_type = None
    monkeypatch.setenv('DB_TYPE', 'sqlite')
    from carrac.content_db import ContentDatabase
    return ContentDatabase(db_path=str(tmp_path / 'test_tags.db'))


# ---------------------------------------------------------------------------
# Tests for _validate_tags helper
# ---------------------------------------------------------------------------

class TestValidateTags:
    def test_valid_list_serialises(self):
        result = _validate_tags(['src:agent', 'strat:bb'])
        assert json.loads(result) == ['src:agent', 'strat:bb']

    def test_empty_list_gets_default_origin(self):
        # No tag list still lands a src: origin tag.
        result = _validate_tags([])
        assert json.loads(result) == ['src:manual']

    def test_non_list_raises(self):
        with pytest.raises(ToolError, match='tags must be a list'):
            _validate_tags('src:agent')

    def test_dict_raises(self):
        with pytest.raises(ToolError, match='tags must be a list'):
            _validate_tags({'src': 'agent'})

    def test_too_many_items_raises(self):
        with pytest.raises(ToolError, match='at most 16'):
            _validate_tags([f'tag{i}' for i in range(17)])

    def test_exactly_16_items_ok(self):
        # The 16-tag budget is the caller's — the reserved src:
        # origin tag is appended on top rather than eating into it, so a full
        # 16 user tags must still be accepted.
        result = _validate_tags([f'tag{i}' for i in range(16)])
        out = json.loads(result)
        assert len(out) == 17
        assert out[:16] == [f'tag{i}' for i in range(16)]
        assert out[16] == 'src:manual'

    def test_16_items_including_own_origin_tag_ok(self):
        raw = [f'tag{i}' for i in range(15)] + ['src:agent']
        out = json.loads(_validate_tags(raw))
        assert len(out) == 16
        assert out[15] == 'src:agent'

    def test_overlong_tag_raises(self):
        long_tag = 'x' * 51
        with pytest.raises(ToolError, match='at most 50 characters'):
            _validate_tags([long_tag])

    def test_exactly_50_chars_ok(self):
        tag = 'x' * 50
        result = _validate_tags([tag])
        assert json.loads(result) == [tag, 'src:manual']

    def test_non_string_item_raises(self):
        with pytest.raises(ToolError, match='each tag must be a string'):
            _validate_tags([42])

    def test_strips_whitespace(self):
        result = _validate_tags(['  src:agent  '])
        assert json.loads(result) == ['src:agent']


class TestOriginTagVocabulary:
    """The src: origin tag is constrained, not free text.

    A typo must not be able to invent a phantom origin, because the
    head-to-head attribution analysis groups on exactly this
    value and would silently split an arm in two.
    """

    @pytest.mark.parametrize('origin', ['agent', 'manual', 'scanner', 'copy'])
    def test_every_known_origin_accepted(self, origin):
        out = json.loads(_validate_tags([f'src:{origin}']))
        assert out == [f'src:{origin}']

    def test_unknown_origin_rejected(self):
        with pytest.raises(ToolError, match='unknown origin tag'):
            _validate_tags(['src:bogus'])

    def test_typo_of_known_origin_rejected(self):
        with pytest.raises(ToolError, match='unknown origin tag'):
            _validate_tags(['src:agnt'])

    def test_two_origin_tags_rejected(self):
        with pytest.raises(ToolError, match='at most one'):
            _validate_tags(['src:agent', 'src:manual'])

    def test_inherit_src_used_when_caller_supplies_none(self):
        out = json.loads(_validate_tags(['strat:new'], inherit_src='src:agent'))
        assert out == ['strat:new', 'src:agent']

    def test_legacy_origin_names_map_to_current_ones(self):
        assert json.loads(_validate_tags(['src:keel'])) == ['src:agent']
        assert json.loads(_validate_tags(['src:coinvest'])) == ['src:copy']
        out = json.loads(_validate_tags(['strat:new'], inherit_src='src:keel'))
        assert out == ['strat:new', 'src:agent']

    def test_caller_origin_wins_over_inherited(self):
        out = json.loads(_validate_tags(['src:copy'], inherit_src='src:agent'))
        assert out == ['src:copy']


# ---------------------------------------------------------------------------
# DB-layer tests: log_trade_open with tags
# ---------------------------------------------------------------------------

class TestDbLayerTags:
    def test_log_trade_open_persists_tags(self, db):
        tags_json = json.dumps(['src:agent'])
        tid = db.log_trade_open(
            user_id=1, asset='BTC', timeframe='1D', direction='long',
            entry_price=65000, tags=tags_json,
        )
        entry = db.get_journal_entry(tid)
        assert entry['tags'] == tags_json

    def test_log_trade_open_without_tags_is_null(self, db):
        tid = db.log_trade_open(
            user_id=1, asset='ETH', timeframe='1D', direction='long',
            entry_price=3000,
        )
        entry = db.get_journal_entry(tid)
        assert entry['tags'] is None

    def test_update_trade_sets_tags(self, db):
        tid = db.log_trade_open(
            user_id=1, asset='BTC', timeframe='1D', direction='long',
            entry_price=65000,
        )
        tags_json = json.dumps(['src:agent', 'strat:pullback'])
        db.trade_update(tid, tags=tags_json)
        entry = db.get_journal_entry(tid)
        assert entry['tags'] == tags_json

    def test_update_trade_clears_tags_with_none(self, db):
        tid = db.log_trade_open(
            user_id=1, asset='BTC', timeframe='1D', direction='long',
            entry_price=65000, tags=json.dumps(['src:agent']),
        )
        db.trade_update(tid, tags=None)
        entry = db.get_journal_entry(tid)
        assert entry['tags'] is None


# ---------------------------------------------------------------------------
# MCP tool handler tests: trade_log with tags
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _bind_user(bind_mcp_user):
    """Bind user_id=42 for all tests in this module."""
    return bind_mcp_user


class TestLogTradeTagsMcp:
    def test_registered(self):
        assert 'trade_log' in TOOL_REGISTRY

    def test_log_trade_no_tags(self, tmp_path, monkeypatch):
        _cdb._db_type = None
        monkeypatch.setenv('DB_TYPE', 'sqlite')
        from carrac import content_db as cdb_mod
        from carrac.content_db import ContentDatabase

        test_db = ContentDatabase(db_path=str(tmp_path / 'mcp_notags.db'))
        monkeypatch.setattr(cdb_mod, 'get_content_db', lambda: test_db)

        fn = TOOL_REGISTRY['trade_log']['handler']
        result = fn({'asset': 'BTC', 'entry_price': 65000})
        tid = result['trade_id']
        entry = test_db.get_journal_entry(tid)
        # An untagged trade is attributed to the human, not left
        # unattributable — nothing may land in the journal without an origin.
        assert json.loads(entry['tags']) == ['src:manual']

    def test_log_trade_with_tags_persists(self, tmp_path, monkeypatch):
        _cdb._db_type = None
        monkeypatch.setenv('DB_TYPE', 'sqlite')
        from carrac import content_db as cdb_mod
        from carrac.content_db import ContentDatabase

        test_db = ContentDatabase(db_path=str(tmp_path / 'mcp_tags.db'))
        monkeypatch.setattr(cdb_mod, 'get_content_db', lambda: test_db)

        fn = TOOL_REGISTRY['trade_log']['handler']
        result = fn({'asset': 'BTC', 'entry_price': 65000, 'tags': ['src:agent']})
        tid = result['trade_id']
        entry = test_db.get_journal_entry(tid)
        assert entry['tags'] is not None
        assert json.loads(entry['tags']) == ['src:agent']

    def test_log_trade_invalid_tags_non_list(self):
        fn = TOOL_REGISTRY['trade_log']['handler']
        with pytest.raises(ToolError, match='tags must be a list'):
            fn({'asset': 'BTC', 'entry_price': 65000, 'tags': 'src:agent'})

    def test_log_trade_tags_too_many(self):
        fn = TOOL_REGISTRY['trade_log']['handler']
        with pytest.raises(ToolError, match='at most 16'):
            fn({'asset': 'BTC', 'entry_price': 65000,
                'tags': [f'tag{i}' for i in range(17)]})

    def test_log_trade_tag_too_long(self):
        fn = TOOL_REGISTRY['trade_log']['handler']
        with pytest.raises(ToolError, match='at most 50 characters'):
            fn({'asset': 'BTC', 'entry_price': 65000, 'tags': ['x' * 51]})


# ---------------------------------------------------------------------------
# MCP tool handler tests: trade_update with tags
# ---------------------------------------------------------------------------

class TestUpdateTradeTagsMcp:
    def test_registered(self):
        assert 'trade_update' in TOOL_REGISTRY

    def _make_db(self, tmp_path, monkeypatch, db_name='upd_tags.db'):
        _cdb._db_type = None
        monkeypatch.setenv('DB_TYPE', 'sqlite')
        from carrac import content_db as cdb_mod
        from carrac.content_db import ContentDatabase
        test_db = ContentDatabase(db_path=str(tmp_path / db_name))
        monkeypatch.setattr(cdb_mod, 'get_content_db', lambda: test_db)
        return test_db

    def test_update_trade_sets_tags(self, tmp_path, monkeypatch):
        test_db = self._make_db(tmp_path, monkeypatch, 'set_tags.db')
        # Seed a trade owned by user_id=42 (bound by bind_mcp_user fixture)
        tid = test_db.log_trade_open(
            user_id=42, asset='BTC', timeframe='1D', direction='long',
            entry_price=65000,
        )
        fn = TOOL_REGISTRY['trade_update']['handler']
        out = fn({'trade_id': tid, 'tags': ['src:agent', 'strat:pullback']})
        assert out['ok'] is True
        entry = test_db.get_journal_entry(tid)
        assert json.loads(entry['tags']) == ['src:agent', 'strat:pullback']

    def test_update_trade_clears_tags_empty_list(self, tmp_path, monkeypatch):
        test_db = self._make_db(tmp_path, monkeypatch, 'clear_tags.db')
        tid = test_db.log_trade_open(
            user_id=42, asset='BTC', timeframe='1D', direction='long',
            entry_price=65000, tags=json.dumps(['src:agent']),
        )
        fn = TOOL_REGISTRY['trade_update']['handler']
        out = fn({'trade_id': tid, 'tags': []})
        assert out['ok'] is True
        entry = test_db.get_journal_entry(tid)
        # Clearing tags must NOT orphan the row's attribution —
        # the original src:agent survives the wipe.
        assert json.loads(entry['tags']) == ['src:agent']

    def test_update_trade_invalid_tags_rejected(self, tmp_path, monkeypatch):
        test_db = self._make_db(tmp_path, monkeypatch, 'invalid_tags.db')
        tid = test_db.log_trade_open(
            user_id=42, asset='ETH', timeframe='4H', direction='short',
            entry_price=3000,
        )
        fn = TOOL_REGISTRY['trade_update']['handler']
        with pytest.raises(ToolError, match='tags must be a list'):
            fn({'trade_id': tid, 'tags': 'not-a-list'})

    def test_update_trade_tag_too_long_rejected(self, tmp_path, monkeypatch):
        test_db = self._make_db(tmp_path, monkeypatch, 'long_tag.db')
        tid = test_db.log_trade_open(
            user_id=42, asset='SOL', timeframe='1D', direction='long',
            entry_price=150,
        )
        fn = TOOL_REGISTRY['trade_update']['handler']
        with pytest.raises(ToolError, match='at most 50 characters'):
            fn({'trade_id': tid, 'tags': ['x' * 51]})


class TestTagsComeBackAsLists:
    """The tags column holds JSON text; tool results hand back the list."""

    def _make_db(self, tmp_path, monkeypatch, name):
        _cdb._db_type = None
        monkeypatch.setenv('DB_TYPE', 'sqlite')
        from carrac import content_db as cdb_mod
        from carrac.content_db import ContentDatabase

        test_db = ContentDatabase(db_path=str(tmp_path / name))
        monkeypatch.setattr(cdb_mod, 'get_content_db', lambda: test_db)
        return test_db

    def test_trade_list_returns_tags_as_a_list(self, tmp_path, monkeypatch, bind_mcp_user):
        self._make_db(tmp_path, monkeypatch, 'list_tags.db')
        bind_mcp_user()
        TOOL_REGISTRY['trade_log']['handler'](
            {'asset': 'BTC', 'entry_price': 65000, 'tags': ['src:agent', 'strat:bb']})

        trades = TOOL_REGISTRY['trade_list']['handler']({'status': 'open'})['trades']

        assert trades[0]['tags'] == ['src:agent', 'strat:bb']

    def test_trade_update_returns_tags_as_a_list(self, tmp_path, monkeypatch, bind_mcp_user):
        self._make_db(tmp_path, monkeypatch, 'update_tags.db')
        bind_mcp_user()
        tid = TOOL_REGISTRY['trade_log']['handler'](
            {'asset': 'ETH', 'entry_price': 3000})['trade_id']

        out = TOOL_REGISTRY['trade_update']['handler'](
            {'trade_id': tid, 'tags': ['src:agent', 'strat:range']})

        assert out['entry']['tags'] == ['src:agent', 'strat:range']
