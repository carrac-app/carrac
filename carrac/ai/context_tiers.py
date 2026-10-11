"""
3-tier context assembly — unified entry point for all AI features.

Tier 1 (always-on,  ~300 tok):  regime, F&G, BTC/ETH/SPX/GOLD state, breadth
Tier 2 (page-aware,  ~500-1500 tok): auto-selected by page_context, or user recipe
Tier 3 (on-demand,  ~200-800 tok): keyword-triggered extras from user message
"""

import logging
from typing import Tuple

from carrac.ai.context_blocks import BLOCK_REGISTRY, estimate_tokens
from carrac.ai.context_composer import TOKEN_BUDGETS, TIER_ORDER, compose_context

logger = logging.getLogger('carrac.ai.context_tiers')

# Block IDs to auto-include per page context (Tier 2)
PAGE_BLOCKS = {
    'chart': [
        {'id': 'price', 'priority': 1},
        {'id': 'ohlcv', 'priority': 2},
        {'id': 'indicators', 'priority': 3},
        {'id': 'structure', 'priority': 4},
        {'id': 'cpr', 'priority': 5},
        {'id': 'drawings', 'priority': 6},
        {'id': 'scenario', 'priority': 7},
        {'id': 'plays', 'priority': 8},
        {'id': 'profile', 'priority': 10},
    ],
    'scanner': [
        {'id': 'price', 'priority': 1},
        {'id': 'indicators', 'priority': 2},
        {'id': 'scorer', 'priority': 3},
        {'id': 'profile', 'priority': 10},
    ],
    'report': [
        {'id': 'morning_report', 'priority': 1},
        {'id': 'macro', 'priority': 2},
        {'id': 'regime', 'priority': 3},
        {'id': 'profile', 'priority': 10},
    ],
    'dashboard': [
        {'id': 'profile', 'priority': 10},
    ],
    'consensio': [
        {'id': 'price', 'priority': 1},
        {'id': 'indicators', 'priority': 2},
        {'id': 'structure', 'priority': 3},
        {'id': 'cpr', 'priority': 4},
        {'id': 'profile', 'priority': 10},
    ],
    'detail': [
        {'id': 'price', 'priority': 1},
        {'id': 'ohlcv', 'priority': 2},
        {'id': 'indicators', 'priority': 3},
        {'id': 'structure', 'priority': 4},
        {'id': 'cpr', 'priority': 5},
        {'id': 'key_levels', 'priority': 6},
        {'id': 'drawings', 'priority': 7},
        {'id': 'profile', 'priority': 10},
    ],
    'journal': [
        {'id': 'journal_history', 'priority': 1},
        {'id': 'profile', 'priority': 10},
    ],
    'plays': [
        {'id': 'plays', 'priority': 1},
        {'id': 'price', 'priority': 2},
        {'id': 'indicators', 'priority': 3},
        {'id': 'scenario', 'priority': 4},
        {'id': 'profile', 'priority': 10},
    ],
    'positions': [
        {'id': 'price', 'priority': 1},
        {'id': 'drawings', 'priority': 2},
        {'id': 'indicators', 'priority': 3},
        {'id': 'plays', 'priority': 4},
        {'id': 'profile', 'priority': 10},
    ],
    'alerts': [
        {'id': 'alerts', 'priority': 1},
        {'id': 'price', 'priority': 2},
        {'id': 'key_levels', 'priority': 3},
        {'id': 'indicators', 'priority': 4},
        {'id': 'profile', 'priority': 10},
    ],
    'stream': [
        {'id': 'price', 'priority': 1},
        {'id': 'indicators', 'priority': 2},
        {'id': 'key_levels', 'priority': 3},
        {'id': 'profile', 'priority': 10},
    ],
    'backtest': [
        {'id': 'price', 'priority': 1},
        {'id': 'ohlcv', 'priority': 2},
        {'id': 'indicators', 'priority': 3},
        {'id': 'profile', 'priority': 10},
    ],
    'news': [
        {'id': 'plays', 'priority': 1},
        {'id': 'regime', 'priority': 2},
        {'id': 'macro', 'priority': 3},
        {'id': 'sentiment', 'priority': 4},
        {'id': 'profile', 'priority': 10},
    ],
}

# Keyword patterns that trigger on-demand blocks (Tier 3)
HINT_KEYWORDS = {
    'scorer': ('score', 'rate this', 'how strong', 'setup check', 'worth trading',
               'setup quality', 'conviction'),
    'morning_report': ('report', 'morning', 'morning analysis', 'today\'s analysis'),
    'correlated_assets': ('correlat', 'spx', 'gold', 'dxy', 'cross-asset',
                          'traditional markets'),
    'sentiment': ('sentiment', 'funding', 'mvrv', 'long ratio', 'short ratio',
                  'open interest'),
    'quant_engine': ('quant', 'hmm', 'anomal', 'z-score', 'regime model',
                     'hidden markov'),
    'breadth': ('breadth', 'how many bullish', 'market breadth'),
    'key_levels': ('levels', 'support', 'resistance', 'pdh', 'pdl', 'pwh', 'pwl'),
    'plays': ('play', 'plays', 'thesis', 'hypothesis', 'hypotheses', 'my plays',
              'how are my plays', 'play status'),
    # Macro keyword triggers so Fed/FOMC/rate queries pull
    # real data into context instead of free-forming from training.
    'macro': ('fed ', 'fed funds', 'fomc', 'powell', 'rate cut', 'rate hike',
              'basis point', 'bps ', 'bp cut', 'bp hike', 'yield curve',
              'cpi', 'inflation print', 'pce', 'ppi', 'nfp', 'jobs report',
              '10y yield', '2y yield', 'treasury yield', 'dollar index'),
    'regime': ('macro regime', 'risk-off', 'risk-on', 'recession',
               'soft landing', 'hard landing'),
}


def detect_context_hints(message: str) -> list:
    """Detect which extra context blocks to auto-include based on message keywords."""
    hints = []
    msg_lower = message.lower()
    for block_id, keywords in HINT_KEYWORDS.items():
        if any(kw in msg_lower for kw in keywords):
            hints.append(block_id)
    return hints


def assemble_context(
    user_id: int,
    asset: str = '',
    timeframe: str = '1D',
    feature: str = 'chat',
    page_context: str = 'chart',
    message: str = '',
    recipe_id: int = None,
    client_drawings: list = None,
    thesis_text: str = '',
    news_context: str = '',
    user_tier: str = 'free',
) -> Tuple[str, int, list]:
    """Assemble 3-tier context for LLM prompt.

    Returns: (assembled_text, token_count, block_summaries)
    """
    budget = TOKEN_BUDGETS.get(user_tier, TOKEN_BUDGETS['free'])
    user_level = TIER_ORDER.get(user_tier, 0)

    parts = []
    token_count = 0
    block_summaries = []

    # ── Tier 1: Always-on (shared macro context) ──
    # Pass user_tier so Premium-only derivatives content doesn't leak into
    # free/member prompts (see context_builder._build_shared_context).
    from carrac.ai.context_builder import get_shared_context
    shared = get_shared_context(user_tier=user_tier)
    if shared:
        shared_tokens = estimate_tokens(shared)
        parts.append(f"--- MARKET STATE ---\n{shared}")
        token_count += shared_tokens
        block_summaries.append({
            'id': '_shared', 'name': 'Market State (always-on)',
            'tokens': shared_tokens, 'included': True, 'tier': 1,
        })

    # ── News article context (injected from frontend) ──
    if news_context:
        import json as _json
        from carrac.grounding import escape_user_text
        try:
            article = _json.loads(news_context)
            # Escape each field individually so injection in a single field
            # can't break out via title/summary content; structured render
            # stays human-readable while quarantined.
            news_block = (
                f"Title: {escape_user_text(article.get('title', 'Unknown'), 'news_title', max_chars=300)}\n"
                f"Source: {escape_user_text(article.get('source', 'Unknown'), 'news_source', max_chars=120)}\n"
                f"Published: {escape_user_text(article.get('published', 'Unknown'), 'news_published', max_chars=80)}\n"
                f"Summary: {escape_user_text(article.get('summary', 'No summary available'), 'news_summary', max_chars=1000)}"
            )
        except (ValueError, TypeError):
            # Plain-text path: wrap whole blob.
            news_block = escape_user_text(news_context, 'news_context', max_chars=1000)
        news_tokens = estimate_tokens(news_block)
        parts.append(f"--- NEWS ARTICLE ---\n{news_block}")
        token_count += news_tokens
        block_summaries.append({
            'id': '_news_article', 'name': 'News Article',
            'tokens': news_tokens, 'included': True, 'tier': 1,
        })

    # ── Tier 2: Page-aware or recipe-based ──

    if recipe_id is not None:
        # User has a recipe — delegate to existing composer
        runtime_overrides = {}
        if client_drawings:
            runtime_overrides['drawings'] = {'_client_drawings': client_drawings}
        runtime_overrides.setdefault('profile', {})['_feature'] = feature
        if thesis_text:
            runtime_overrides.setdefault('thesis', {})['text'] = thesis_text

        composed_text, composed_tokens, composed_summaries = compose_context(
            user_id, recipe_id=recipe_id, asset=asset, timeframe=timeframe,
            thesis_text=thesis_text, runtime_overrides=runtime_overrides,
            user_tier=user_tier,
        )
        if composed_text:
            parts.append(composed_text)
            token_count += composed_tokens
            for bs in composed_summaries:
                bs['tier'] = 2
            block_summaries.extend(composed_summaries)
    else:
        # Auto-select blocks based on page context
        page_blocks = PAGE_BLOCKS.get(page_context, PAGE_BLOCKS['chart'])

        # Feature-specific block overrides
        if feature == 'trade_design':
            page_blocks = [
                {'id': 'price', 'priority': 1},
                {'id': 'indicators', 'priority': 2},
                {'id': 'structure', 'priority': 3},
                {'id': 'cpr', 'priority': 4},
                {'id': 'key_levels', 'priority': 5},
                {'id': 'scorer', 'priority': 6},
                {'id': 'thesis', 'priority': 7},
                {'id': 'drawings', 'priority': 8},
                {'id': 'profile', 'priority': 10},
            ]
        elif feature == 'confluence':
            page_blocks = [
                {'id': 'price', 'priority': 1},
                {'id': 'indicators', 'priority': 2},
                {'id': 'structure', 'priority': 3},
                {'id': 'cpr', 'priority': 4},
                {'id': 'key_levels', 'priority': 5},
                {'id': 'ohlcv', 'priority': 6},
                {'id': 'drawings', 'priority': 7},
                {'id': 'scorer', 'priority': 8},
                {'id': 'profile', 'priority': 10},
            ]
        elif feature == 'report_followup':
            page_blocks = [
                {'id': 'full_report', 'priority': 1},
                {'id': 'macro', 'priority': 2},
                {'id': 'regime', 'priority': 3},
                {'id': 'scenario', 'priority': 5},
                {'id': 'profile', 'priority': 10},
            ]

        for block_config in page_blocks:
            block_id = block_config['id']
            block = BLOCK_REGISTRY.get(block_id)
            if not block:
                continue

            # Tier gate
            required_level = TIER_ORDER.get(block.tier_minimum, 0)
            if user_level < required_level:
                block_summaries.append({
                    'id': block_id, 'name': block.name,
                    'tokens': 0, 'included': False, 'reason': 'locked', 'tier': 2,
                })
                continue

            # Build config
            config = {}
            if block_id == 'drawings' and client_drawings:
                config['_client_drawings'] = client_drawings
            if block_id == 'thesis' and thesis_text:
                config['text'] = thesis_text
            if block_id == 'scorer' and feature == 'trade_design':
                config['_dual'] = True
            if block_id == 'profile':
                config['_feature'] = feature

            try:
                text = block.fetch(user_id, asset, timeframe, config)
            except Exception as e:
                logger.warning(f"Tier 2 block {block_id} error: {e}")
                text = ''

            if not text:
                block_summaries.append({
                    'id': block_id, 'name': block.name,
                    'tokens': 0, 'included': False, 'reason': 'empty', 'tier': 2,
                })
                continue

            tokens = estimate_tokens(text)
            if token_count + tokens > budget - 200:  # Leave room for Tier 3
                block_summaries.append({
                    'id': block_id, 'name': block.name,
                    'tokens': tokens, 'included': False, 'reason': 'budget', 'tier': 2,
                })
                continue

            parts.append(f"--- {block.name.upper()} ---\n{text}")
            token_count += tokens
            block_summaries.append({
                'id': block_id, 'name': block.name,
                'tokens': tokens, 'included': True, 'tier': 2,
            })

    # ── Tier 3: On-demand (keyword-triggered) ──
    hints = detect_context_hints(message)
    # Remove blocks already included in Tier 2
    included_ids = {bs['id'] for bs in block_summaries if bs.get('included')}
    hints = [h for h in hints if h not in included_ids]

    for block_id in hints:
        block = BLOCK_REGISTRY.get(block_id)
        if not block:
            continue

        required_level = TIER_ORDER.get(block.tier_minimum, 0)
        if user_level < required_level:
            continue

        config = {}
        if block_id == 'profile':
            config['_feature'] = feature

        try:
            text = block.fetch(user_id, asset, timeframe, config)
        except Exception as e:
            logger.warning(f"Tier 3 block {block_id} error: {e}")
            text = ''

        if not text:
            continue

        tokens = estimate_tokens(text)
        if token_count + tokens > budget:
            block_summaries.append({
                'id': block_id, 'name': block.name,
                'tokens': tokens, 'included': False, 'reason': 'budget', 'tier': 3,
            })
            continue

        parts.append(f"--- {block.name.upper()} [auto] ---\n{text}")
        token_count += tokens
        block_summaries.append({
            'id': block_id, 'name': block.name,
            'tokens': tokens, 'included': True, 'tier': 3,
        })

    assembled = '\n\n'.join(parts)
    return assembled, token_count, block_summaries
