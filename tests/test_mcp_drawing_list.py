"""Tests for drawing_list — empty-case shape honesty.

Every sibling list tool (play_list -> {'plays': [], 'count': 0}, alert_manage
list -> {'alerts': [], 'count': 0}) returns a list key on the empty case.
drawing_list must not return prose-only ({'text': ..., 'count': 0}) with no
list key at all. The drawing DB collaborator is stubbed; the handler under
test — drawing_list — is never monkeypatched.
"""

import pytest

from carrac.mcp import tools  # noqa: F401 — ensures registration
from carrac.mcp.registry import TOOL_REGISTRY
from carrac.mcp.tools.context import drawing_list


@pytest.fixture(autouse=True)
def _bind_user(bind_mcp_user):
    return bind_mcp_user


class _EmptyDB:
    def get_all_drawings_grouped(self, user_id=None):
        return {}


class _PopulatedDB:
    def get_all_drawings_grouped(self, user_id=None):
        return {
            'BTC': {
                '1D': [
                    {'id': 1, 'type': 'horizontal_line', 'price_level': 50000,
                     'timeframe': '1D', 'label': ''},
                ],
            },
        }


class TestDrawingListRegistered:
    def test_registered(self):
        assert 'drawing_list' in TOOL_REGISTRY


class TestDrawingListEmptyShape:
    def test_empty_case_has_drawings_key(self, app, monkeypatch):
        from carrac import content_db as cdb_mod
        monkeypatch.setattr(cdb_mod, 'get_content_db', lambda: _EmptyDB())

        with app.app_context():
            out = drawing_list({})

        assert 'drawings' in out
        assert out['drawings'] == []
        assert out['count'] == 0

    def test_empty_case_keeps_prose_as_extra_field(self, app, monkeypatch):
        from carrac import content_db as cdb_mod
        monkeypatch.setattr(cdb_mod, 'get_content_db', lambda: _EmptyDB())

        with app.app_context():
            out = drawing_list({})

        assert out['text'] == 'No drawings found.'

class TestDrawingListNonEmpty:
    def test_populated_case_unaffected(self, app, monkeypatch):
        """The non-empty path (line-based text) is out of scope for this fix
        and must keep working exactly as before."""
        from carrac import content_db as cdb_mod
        monkeypatch.setattr(cdb_mod, 'get_content_db', lambda: _PopulatedDB())

        with app.app_context():
            out = drawing_list({})

        assert out['count'] == 1
        assert '[id:1] BTC 1D' in out['text']


class _EnrichDB:
    def get_all_drawings_grouped(self, user_id=None):
        return {
            'BTC': {
                '1D': [
                    {'id': 1, 'type': 'horizontal_line', 'price_level': 50000,
                     'timeframe': '1D', 'label': ''},
                    {'id': 2, 'type': 'zone', 'price_level': 56000,
                     'price_level_upper': 58000, 'timeframe': '1D', 'label': ''},
                    {'id': 3, 'type': 'position', 'entry_price': 50000,
                     'direction': 'long', 'timeframe': '1D', 'label': ''},
                ],
            },
        }


class TestDrawingListEnrich:
    """enrich=true (default) annotates drawings with the current price."""

    def _run(self, app, monkeypatch, args):
        from carrac import content_db as cdb_mod
        from carrac.drawings import routes as drawings_api
        monkeypatch.setattr(cdb_mod, 'get_content_db', lambda: _EnrichDB())
        monkeypatch.setattr(drawings_api, '_get_asset_price', lambda a: 55000.0)
        with app.app_context():
            return drawing_list(args)['text'].splitlines()

    def test_default_annotates_levels_zones_and_positions(self, app, monkeypatch):
        lines = self._run(app, monkeypatch, {})
        assert 'price $55,000.00 is 10.0% above' in lines[0]
        assert 'is 1.8% below zone' in lines[1]
        assert 'P&L: +10.0%' in lines[2]

    def test_enrich_false_leaves_lines_plain(self, app, monkeypatch):
        lines = self._run(app, monkeypatch, {'enrich': False})
        assert all('price $' not in ln and 'P&L' not in ln for ln in lines)
