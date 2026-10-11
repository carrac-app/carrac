"""MCP tool: recipe_manage — CRUD for AI context recipes.

AI context recipes control which data blocks the AI Copilot assembles
when building its analysis context: price action, drawings, plays, macro
data, derivatives, volume profile, etc. Each recipe is a named list of
blocks with per-block enable/priority/config overrides.

Built-in templates (read-only):
  quick_read      — fast overview, minimal context
  swing_trader    — multi-TF analysis with drawings and plays
  scalper         — LTF focused, tight context
  macro_overlay   — heavy on macro/derivatives data
  full_context    — everything enabled
  validation      — structured for play validation
"""

import logging

from carrac.mcp.registry import mcp_tool
from carrac.mcp.protocol import ToolError
from carrac.mcp.tools import get_mcp_user_id

logger = logging.getLogger('carrac.mcp.recipes')

_VALID_TEMPLATE_IDS = {
    'quick_read', 'swing_trader', 'scalper',
    'macro_overlay', 'full_context', 'validation',
}


@mcp_tool(
    'recipe_manage',
    'CRUD for AI context recipes — named configurations that control which data '
    'blocks the AI Copilot assembles when analysing a chart (price action, drawings, '
    'plays, macro, derivatives, volume profile, etc.). '
    'Supported actions: '
    'list — return all recipes owned by the caller; '
    'list_templates — return the 6 read-only built-in templates '
    '(quick_read, swing_trader, scalper, macro_overlay, full_context, validation); '
    'create — create a new recipe; optionally clone blocks from a template_id '
    '(if template_id supplied and no name given, the template name is used); '
    'update — patch name, blocks, and/or is_default on an existing recipe '
    '(recipe_id required; omitted fields are unchanged); '
    'delete — permanently remove a recipe owned by the caller (recipe_id required; '
    'raises error if not found or not owned). '
    'SIDE EFFECT: create/update/delete persist to the user recipe store. '
    'Setting is_default=true on one recipe automatically clears is_default on others. '
    'Returns the full recipe object for create/update, {ok, deleted_recipe_id} for delete. '
    'Each block in blocks has: {id: str, enabled: bool, priority: int, config: object}. '
    'EXAMPLE create from template: '
    '{"action": "create", "template_id": "swing_trader", "is_default": true} '
    'EXAMPLE update: {"action": "update", "recipe_id": 5, "name": "My Swing Setup"}',
    {
        'type': 'object',
        'properties': {
            'action': {
                'type': 'string',
                'enum': ['list', 'list_templates', 'create', 'update', 'delete'],
                'description': 'Operation to perform (required)',
            },
            'recipe_id': {
                'type': 'integer',
                'description': 'Recipe ID — required for update and delete',
            },
            'name': {
                'type': 'string',
                'description': 'Recipe name (max 100 chars) — for create and update',
            },
            'blocks': {
                'type': 'array',
                'description': (
                    'Ordered list of context blocks. Each block: '
                    '{id: str, enabled: bool, priority: int, config: object}'
                ),
                'items': {
                    'type': 'object',
                    'properties': {
                        'id': {'type': 'string', 'description': 'Block identifier'},
                        'enabled': {'type': 'boolean'},
                        'priority': {'type': 'integer'},
                        'config': {'type': 'object'},
                    },
                    'required': ['id'],
                },
            },
            'is_default': {
                'type': 'boolean',
                'description': (
                    'Set this recipe as the default for AI Copilot context. '
                    'Clearing another recipe\'s default is handled automatically.'
                ),
            },
            'template_id': {
                'type': 'string',
                'enum': sorted(_VALID_TEMPLATE_IDS),
                'description': (
                    'Clone from a built-in template when creating. '
                    'If supplied and no name is given, the template name is used.'
                ),
            },
        },
        'required': ['action'],
    },
    min_tier='member',
)
def recipe_manage(arguments):
    from carrac.ai.context_composer import (
        list_recipes,
        get_templates,
        create_recipe,
        update_recipe,
        delete_recipe,
        load_recipe,
    )

    user_id = get_mcp_user_id()
    action = arguments.get('action')

    if action == 'list':
        recipes = list_recipes(user_id)
        return {'recipes': recipes, 'count': len(recipes)}

    elif action == 'list_templates':
        return {'templates': get_templates()}

    elif action == 'create':
        name = (arguments.get('name') or '').strip()
        blocks = arguments.get('blocks') or []
        is_default = bool(arguments.get('is_default', False))
        template_id = arguments.get('template_id') or None

        if template_id and template_id not in _VALID_TEMPLATE_IDS:
            raise ToolError(
                f'Invalid template_id "{template_id}". '
                f'Valid: {sorted(_VALID_TEMPLATE_IDS)}'
            )

        # name is required unless a template provides one
        if not name and not template_id:
            raise ToolError('name is required when not using a template_id')

        if not isinstance(blocks, list):
            raise ToolError('blocks must be an array')

        try:
            recipe_id = create_recipe(
                user_id=user_id,
                name=name,
                blocks=blocks,
                is_default=is_default,
                template_id=template_id,
            )
        except Exception as exc:
            logger.error('create_recipe failed: %s', exc)
            raise ToolError(f'Failed to create recipe: {exc}')

        if not recipe_id:
            raise ToolError('Recipe creation failed — no ID returned')

        recipe = load_recipe(user_id, recipe_id)
        return {'recipe_id': recipe_id, **(recipe or {})}

    elif action == 'update':
        recipe_id = arguments.get('recipe_id')
        if recipe_id is None:
            raise ToolError('recipe_id is required for update')
        try:
            recipe_id = int(recipe_id)
        except (TypeError, ValueError):
            raise ToolError('recipe_id must be an integer')

        name = arguments.get('name') or None
        if name:
            name = name.strip() or None

        blocks = arguments.get('blocks')  # None = don't change
        is_default = arguments.get('is_default')  # None = don't change

        if blocks is not None and not isinstance(blocks, list):
            raise ToolError('blocks must be an array')

        try:
            ok = update_recipe(
                user_id=user_id,
                recipe_id=recipe_id,
                name=name,
                blocks=blocks,
                is_default=is_default,
            )
        except Exception as exc:
            logger.error('update_recipe failed: %s', exc)
            raise ToolError(f'Failed to update recipe: {exc}')

        if not ok:
            raise ToolError(
                f'Recipe {recipe_id} not found or not owned by caller'
            )

        recipe = load_recipe(user_id, recipe_id)
        return {'ok': True, 'recipe': recipe}

    elif action == 'delete':
        recipe_id = arguments.get('recipe_id')
        if recipe_id is None:
            raise ToolError('recipe_id is required for delete')
        try:
            recipe_id = int(recipe_id)
        except (TypeError, ValueError):
            raise ToolError('recipe_id must be an integer')

        try:
            deleted = delete_recipe(user_id=user_id, recipe_id=recipe_id)
        except Exception as exc:
            logger.error('delete_recipe failed: %s', exc)
            raise ToolError(f'Failed to delete recipe: {exc}')

        if not deleted:
            raise ToolError(
                f'Recipe {recipe_id} not found or not owned by caller'
            )

        return {'ok': True, 'deleted_recipe_id': recipe_id}

    else:
        raise ToolError(
            f'Unknown action "{action}". '
            f'Valid: list, list_templates, create, update, delete'
        )
