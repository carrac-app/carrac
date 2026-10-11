"""Tests for recipe_manage — list-action count honesty fix.

action=list returns a count key alongside {'recipes': [...]}, like
every other list-shaped MCP tool (watchlist_manage, play_list, alert_manage
list, drawing_list). The context_composer collaborator is stubbed; the
handler under test — recipe_manage — is never monkeypatched.
"""

import pytest

from carrac.mcp import tools  # noqa: F401 — ensures registration
from carrac.mcp.registry import TOOL_REGISTRY
from carrac.mcp.tools.recipes import recipe_manage


@pytest.fixture(autouse=True)
def _bind_user(bind_mcp_user):
    return bind_mcp_user


class TestRecipeManageRegistered:
    def test_registered(self):
        assert 'recipe_manage' in TOOL_REGISTRY


class TestRecipeListCount:
    def test_empty_list_has_count_zero(self, app, monkeypatch):
        import carrac.ai.context_composer as cc
        monkeypatch.setattr(cc, 'list_recipes', lambda user_id: [])

        with app.app_context():
            out = recipe_manage({'action': 'list'})

        assert out == {'recipes': [], 'count': 0}

    def test_populated_list_count_matches_length(self, app, monkeypatch):
        import carrac.ai.context_composer as cc
        recipes = [
            {'id': 1, 'name': 'Swing'},
            {'id': 2, 'name': 'Scalp'},
            {'id': 3, 'name': 'Macro'},
        ]
        monkeypatch.setattr(cc, 'list_recipes', lambda user_id: list(recipes))

        with app.app_context():
            out = recipe_manage({'action': 'list'})

        assert out['count'] == 3
        assert out['recipes'] == recipes
