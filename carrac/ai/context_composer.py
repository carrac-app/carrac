"""
Context Composer — assembles LLM context from user's recipe.

A recipe is a saved configuration of which context blocks to include,
in what order (priority), and with what settings.
"""

import json
import logging
from typing import Optional, Tuple

from carrac.ai.context_blocks import BLOCK_REGISTRY, estimate_tokens

logger = logging.getLogger('carrac.ai.context_composer')

# Token budgets per tier
TOKEN_BUDGETS = {
    'free': 4000,
    'member': 8000,
    'tester': 8000,
    'premium': 12000,
    'admin': 20000,
}

# Tier ordering for access control
TIER_ORDER = {'free': 0, 'member': 1, 'tester': 1, 'premium': 2, 'admin': 99}

# ---------------------------------------------------------------------------
# Templates — pre-built recipes users can clone
# ---------------------------------------------------------------------------

TEMPLATES = {
    'quick_read': {
        'name': 'Quick Read',
        'description': 'Fast overview — price, indicators, structure, regime, drawings',
        'blocks': [
            {'id': 'price', 'enabled': True, 'priority': 1, 'config': {}},
            {'id': 'indicators', 'enabled': True, 'priority': 2, 'config': {}},
            {'id': 'structure', 'enabled': True, 'priority': 3, 'config': {}},
            {'id': 'regime', 'enabled': True, 'priority': 4, 'config': {}},
            {'id': 'drawings', 'enabled': True, 'priority': 5, 'config': {}},
        ],
    },
    'swing_trader': {
        'name': 'Swing Trader',
        'description': 'Multi-day holds — OHLCV, multi-TF, macro, CPR, drawings, thesis',
        'blocks': [
            {'id': 'ohlcv', 'enabled': True, 'priority': 1, 'config': {'lookback_bars': 30}},
            {'id': 'multi_tf', 'enabled': True, 'priority': 2, 'config': {}},
            {'id': 'macro', 'enabled': True, 'priority': 3, 'config': {}},
            {'id': 'cpr', 'enabled': True, 'priority': 4, 'config': {}},
            {'id': 'drawings', 'enabled': True, 'priority': 5, 'config': {}},
            {'id': 'thesis', 'enabled': True, 'priority': 6, 'config': {}},
            {'id': 'profile', 'enabled': True, 'priority': 7, 'config': {}},
        ],
    },
    'scalper': {
        'name': 'Scalper',
        'description': 'Intraday — OHLCV, indicators, sentiment, key levels, volume profile',
        'blocks': [
            {'id': 'price', 'enabled': True, 'priority': 1, 'config': {}},
            {'id': 'ohlcv', 'enabled': True, 'priority': 2, 'config': {'lookback_bars': 100}},
            {'id': 'indicators', 'enabled': True, 'priority': 3, 'config': {}},
            {'id': 'sentiment', 'enabled': True, 'priority': 4, 'config': {}},
            {'id': 'key_levels', 'enabled': True, 'priority': 5, 'config': {}},
            {'id': 'volume_profile', 'enabled': True, 'priority': 6, 'config': {}},
        ],
    },
    'macro_overlay': {
        'name': 'Macro Overlay',
        'description': 'Macro-first — macro, regime, breadth, morning report, OHLCV, thesis',
        'blocks': [
            {'id': 'macro', 'enabled': True, 'priority': 1, 'config': {}},
            {'id': 'regime', 'enabled': True, 'priority': 2, 'config': {}},
            {'id': 'breadth', 'enabled': True, 'priority': 3, 'config': {}},
            {'id': 'morning_report', 'enabled': True, 'priority': 4, 'config': {}},
            {'id': 'ohlcv', 'enabled': True, 'priority': 5, 'config': {'lookback_bars': 7}},
            {'id': 'thesis', 'enabled': True, 'priority': 6, 'config': {}},
        ],
    },
    'full_context': {
        'name': 'Full Context',
        'description': 'Kitchen sink — all blocks enabled, priority-ordered',
        'blocks': [
            {'id': 'price', 'enabled': True, 'priority': 1, 'config': {}},
            {'id': 'ohlcv', 'enabled': True, 'priority': 2, 'config': {}},
            {'id': 'indicators', 'enabled': True, 'priority': 3, 'config': {}},
            {'id': 'structure', 'enabled': True, 'priority': 4, 'config': {}},
            {'id': 'cpr', 'enabled': True, 'priority': 5, 'config': {}},
            {'id': 'macro', 'enabled': True, 'priority': 6, 'config': {}},
            {'id': 'regime', 'enabled': True, 'priority': 7, 'config': {}},
            {'id': 'drawings', 'enabled': True, 'priority': 8, 'config': {}},
            {'id': 'scenario', 'enabled': True, 'priority': 9, 'config': {}},
            {'id': 'sentiment', 'enabled': True, 'priority': 10, 'config': {}},
            {'id': 'breadth', 'enabled': True, 'priority': 11, 'config': {}},
            {'id': 'profile', 'enabled': True, 'priority': 12, 'config': {}},
            {'id': 'morning_report', 'enabled': True, 'priority': 13, 'config': {}},
            {'id': 'multi_tf', 'enabled': True, 'priority': 14, 'config': {}},
            {'id': 'alerts', 'enabled': True, 'priority': 15, 'config': {}},
            {'id': 'thesis', 'enabled': True, 'priority': 16, 'config': {}},
            {'id': 'correlated_assets', 'enabled': True, 'priority': 17, 'config': {}},
        ],
    },
    'validation': {
        'name': 'Validation',
        'description': 'Current behavior — indicators, structure, CPR, macro, scenario, profile',
        'blocks': [
            {'id': 'indicators', 'enabled': True, 'priority': 1, 'config': {}},
            {'id': 'consensio', 'enabled': True, 'priority': 2, 'config': {}},
            {'id': 'structure', 'enabled': True, 'priority': 3, 'config': {}},
            {'id': 'cpr', 'enabled': True, 'priority': 4, 'config': {}},
            {'id': 'macro', 'enabled': True, 'priority': 5, 'config': {}},
            {'id': 'regime', 'enabled': True, 'priority': 6, 'config': {}},
            {'id': 'scenario', 'enabled': True, 'priority': 7, 'config': {}},
            {'id': 'profile', 'enabled': True, 'priority': 8, 'config': {}},
        ],
    },
}


def get_templates() -> list:
    """Return template list for the UI."""
    return [
        {'id': tid, 'name': t['name'], 'description': t['description'],
         'block_count': len([b for b in t['blocks'] if b['enabled']])}
        for tid, t in TEMPLATES.items()
    ]


# ---------------------------------------------------------------------------
# Recipe DB operations
# ---------------------------------------------------------------------------

def load_recipe(user_id: int, recipe_id: int) -> Optional[dict]:
    """Load a recipe from the database."""
    from carrac.db_pool import get_conn, adapt_query
    with get_conn() as conn:
        cursor = conn.cursor()
        cursor.execute(adapt_query(
            'SELECT id, user_id, name, is_default, blocks, created_at, updated_at '
            'FROM context_recipes WHERE id = ? AND user_id = ?'
        ), (recipe_id, user_id))
        row = cursor.fetchone()
        if not row:
            return None
        return _row_to_recipe(row)


def load_default_recipe(user_id: int) -> Optional[dict]:
    """Load the user's default recipe."""
    from carrac.db_pool import get_conn, adapt_query
    with get_conn() as conn:
        cursor = conn.cursor()
        cursor.execute(adapt_query(
            'SELECT id, user_id, name, is_default, blocks, created_at, updated_at '
            'FROM context_recipes WHERE user_id = ? AND is_default = TRUE'
        ), (user_id,))
        row = cursor.fetchone()
        if not row:
            return None
        return _row_to_recipe(row)


def list_recipes(user_id: int) -> list:
    """List all recipes for a user."""
    from carrac.db_pool import get_conn, adapt_query
    with get_conn() as conn:
        cursor = conn.cursor()
        cursor.execute(adapt_query(
            'SELECT id, user_id, name, is_default, blocks, created_at, updated_at '
            'FROM context_recipes WHERE user_id = ? ORDER BY is_default DESC, updated_at DESC'
        ), (user_id,))
        return [_row_to_recipe(r) for r in cursor.fetchall()]


def create_recipe(user_id: int, name: str, blocks: list, is_default: bool = False,
                  template_id: str = None) -> Optional[int]:
    """Create a new recipe. If template_id provided, clone from template."""
    from carrac.db_pool import get_conn, adapt_query, returning_id, get_last_id
    from datetime import datetime, timezone

    if template_id and template_id in TEMPLATES:
        template = TEMPLATES[template_id]
        if not name:
            name = template['name']
        blocks = template['blocks']

    blocks_json = json.dumps(blocks)

    with get_conn() as conn:
        cursor = conn.cursor()

        # If setting as default, clear existing default
        if is_default:
            cursor.execute(adapt_query(
                'UPDATE context_recipes SET is_default = FALSE WHERE user_id = ?'
            ), (user_id,))

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        ret = returning_id()
        cursor.execute(adapt_query(
            f'INSERT INTO context_recipes (user_id, name, is_default, blocks, created_at, updated_at) '
            f'VALUES (?, ?, ?, ?, ?, ?) {ret}'
        ), (user_id, name[:100], is_default, blocks_json, now, now))

        recipe_id = get_last_id(cursor)
        conn.commit()
        return recipe_id


def update_recipe(user_id: int, recipe_id: int, name: str = None,
                  blocks: list = None, is_default: bool = None) -> bool:
    """Update an existing recipe."""
    from carrac.db_pool import get_conn, adapt_query
    from datetime import datetime, timezone

    with get_conn() as conn:
        cursor = conn.cursor()

        # Verify ownership
        cursor.execute(adapt_query(
            'SELECT id FROM context_recipes WHERE id = ? AND user_id = ?'
        ), (recipe_id, user_id))
        if not cursor.fetchone():
            return False

        updates = []
        params = []

        if name is not None:
            updates.append('name = ?')
            params.append(name[:100])

        if blocks is not None:
            updates.append('blocks = ?')
            params.append(json.dumps(blocks))

        if is_default is True:
            # Clear existing default first
            cursor.execute(adapt_query(
                'UPDATE context_recipes SET is_default = FALSE WHERE user_id = ?'
            ), (user_id,))
            updates.append('is_default = TRUE')

        if is_default is False:
            updates.append('is_default = FALSE')

        if not updates:
            return True

        updates.append('updated_at = ?')
        params.append(datetime.now(timezone.utc).replace(tzinfo=None))
        params.extend([recipe_id, user_id])

        sql = f'UPDATE context_recipes SET {", ".join(updates)} WHERE id = ? AND user_id = ?'
        cursor.execute(adapt_query(sql), tuple(params))
        conn.commit()
        return True


def delete_recipe(user_id: int, recipe_id: int) -> bool:
    """Delete a recipe."""
    from carrac.db_pool import get_conn, adapt_query
    with get_conn() as conn:
        cursor = conn.cursor()
        cursor.execute(adapt_query(
            'DELETE FROM context_recipes WHERE id = ? AND user_id = ?'
        ), (recipe_id, user_id))
        deleted = cursor.rowcount > 0
        conn.commit()
        return deleted


def _row_to_recipe(row) -> dict:
    """Convert a DB row to a recipe dict."""
    blocks_raw = row['blocks'] if isinstance(row, dict) else row[4]
    if isinstance(blocks_raw, str):
        blocks = json.loads(blocks_raw)
    else:
        blocks = blocks_raw or []

    return {
        'id': row['id'] if isinstance(row, dict) else row[0],
        'user_id': row['user_id'] if isinstance(row, dict) else row[1],
        'name': row['name'] if isinstance(row, dict) else row[2],
        'is_default': row['is_default'] if isinstance(row, dict) else row[3],
        'blocks': blocks,
    }


# ---------------------------------------------------------------------------
# Prompt assembly
# ---------------------------------------------------------------------------

def compose_context(user_id: int, recipe_id: int = None, asset: str = '',
                    timeframe: str = '1D', thesis_text: str = '',
                    runtime_overrides: dict = None,
                    user_tier: str = 'free') -> Tuple[str, int, list]:
    """Assemble LLM context from user's recipe.

    Returns: (assembled_text, token_count, block_summaries)
    block_summaries: list of {'id', 'name', 'tokens', 'included'} for UI.
    """
    # Load recipe (or fall back to default → quick_read template)
    recipe = None
    if recipe_id:
        recipe = load_recipe(user_id, recipe_id)
    if not recipe:
        recipe = load_default_recipe(user_id)
    if not recipe:
        # No saved recipe — use Quick Read template
        recipe = {'blocks': TEMPLATES['quick_read']['blocks']}

    overrides = runtime_overrides or {}
    user_level = TIER_ORDER.get(user_tier, 0)
    budget = TOKEN_BUDGETS.get(user_tier, TOKEN_BUDGETS['free'])

    # Filter to enabled blocks, sorted by priority
    enabled_blocks = [b for b in recipe['blocks'] if b.get('enabled', True)]
    enabled_blocks.sort(key=lambda b: b.get('priority', 999))

    parts = []
    token_count = 0
    block_summaries = []

    for block_config in enabled_blocks:
        block_id = block_config['id']
        block = BLOCK_REGISTRY.get(block_id)
        if not block:
            continue

        # Tier gate
        required_level = TIER_ORDER.get(block.tier_minimum, 0)
        if user_level < required_level:
            block_summaries.append({
                'id': block_id, 'name': block.name,
                'tokens': 0, 'included': False, 'reason': 'locked',
            })
            continue

        # Build config: merge block default + recipe config + runtime overrides
        config = dict(block_config.get('config', {}))
        config.update(overrides.get(block_id, {}))

        # Inject thesis text into thesis block config
        if block_id == 'thesis' and thesis_text:
            config['text'] = thesis_text

        try:
            text = block.fetch(user_id, asset, timeframe, config)
        except Exception as e:
            logger.warning(f"Block {block_id} fetch error: {e}")
            text = ''

        if not text:
            block_summaries.append({
                'id': block_id, 'name': block.name,
                'tokens': 0, 'included': False, 'reason': 'empty',
            })
            continue

        tokens = estimate_tokens(text)

        if token_count + tokens > budget:
            block_summaries.append({
                'id': block_id, 'name': block.name,
                'tokens': tokens, 'included': False, 'reason': 'budget',
            })
            continue

        parts.append(f"--- {block.name.upper()} ---\n{text}")
        token_count += tokens
        block_summaries.append({
            'id': block_id, 'name': block.name,
            'tokens': tokens, 'included': True,
        })

    assembled = '\n\n'.join(parts)
    return assembled, token_count, block_summaries


def preview_context(user_id: int, recipe_id: int = None, asset: str = '',
                    timeframe: str = '1D', thesis_text: str = '',
                    user_tier: str = 'free') -> dict:
    """Preview what the assembled context would look like. Returns dict for API."""
    text, tokens, summaries = compose_context(
        user_id, recipe_id, asset, timeframe, thesis_text, user_tier=user_tier,
    )
    return {
        'text': text,
        'total_tokens': tokens,
        'budget': TOKEN_BUDGETS.get(user_tier, TOKEN_BUDGETS['free']),
        'blocks': summaries,
    }
