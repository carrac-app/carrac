"""
Copilot tool definitions — scopes MCP tools per focus area for AI chat.

Read tools auto-execute; write tools use propose+confirm pattern.
Lazy loading: tools only included when message keywords suggest data needs.
"""

import json
import logging

logger = logging.getLogger('carrac.ai.copilot_tools')

# ── Tool classification ──

READ_TOOLS = frozenset({
    'market_get_chart', 'market_get_detail', 'market_get_overview', 'market_search_symbol',
    'market_get_sentiment', 'market_get_calendar', 'scanner_get_screen',
    'scanner_get_confluence_zones', 'scanner_get_transitions', 'scanner_get_live_stream',
    'drawing_list', 'util_batch_query', 'play_list', 'play_get_digest',
    'play_evaluate', 'trade_get_positions', 'trade_get_analytics',
    'alert_list_triggered', 'report_get_compact', 'quant_get_data',
    'quant_get_correlations', 'backtest_get_historical_analog', 'get_guide',
})

WRITE_TOOLS = frozenset({
    'alert_manage', 'play_create', 'play_update', 'play_archive', 'trade_log',
})

# ── Tools available per copilot focus area ──

FOCUS_TOOLS = {
    'chart': [
        'market_get_chart', 'market_get_detail', 'scanner_get_screen', 'market_get_sentiment',
        'drawing_list', 'scanner_get_confluence_zones', 'play_list', 'trade_get_positions',
        'alert_manage', 'play_create', 'trade_log', 'get_guide',
    ],
    'journal': [
        'trade_get_analytics', 'market_get_chart', 'play_list', 'trade_log',
        'get_guide',
    ],
    'plays': [
        'play_list', 'play_get_digest', 'play_evaluate', 'play_create',
        'play_update', 'play_archive', 'market_get_chart', 'scanner_get_screen',
        'get_guide',
    ],
    'report': [
        'report_get_compact', 'market_get_chart', 'play_get_digest', 'market_get_sentiment',
        'quant_get_data', 'get_guide',
    ],
    'positions': [
        'trade_get_positions', 'market_get_chart', 'scanner_get_confluence_zones',
        'play_list', 'alert_manage', 'trade_log', 'get_guide',
    ],
    'alerts': [
        'alert_list_triggered', 'scanner_get_confluence_zones', 'market_get_chart',
        'alert_manage', 'get_guide',
    ],
    'scanner': [
        'scanner_get_screen', 'market_get_chart', 'market_get_detail',
        'scanner_get_confluence_zones', 'scanner_get_transitions', 'get_guide',
    ],
    'stream': [
        'scanner_get_live_stream', 'scanner_get_transitions', 'market_get_chart',
        'scanner_get_confluence_zones', 'get_guide',
    ],
    'backtest': [
        'backtest_get_historical_analog', 'market_get_chart', 'quant_get_data', 'get_guide',
    ],
    'dashboard': [
        'market_get_chart', 'market_get_sentiment', 'scanner_get_screen', 'get_guide',
    ],
    'consensio': [
        'market_get_chart', 'scanner_get_screen', 'market_get_detail', 'market_get_sentiment',
        'scanner_get_transitions', 'get_guide',
    ],
    'detail': [
        'market_get_chart', 'market_get_detail', 'scanner_get_screen', 'market_get_sentiment',
        'scanner_get_confluence_zones', 'drawing_list', 'get_guide',
    ],
    'news': [
        'market_get_chart', 'market_get_detail', 'market_search_symbol',
        'play_list', 'play_get_digest', 'trade_get_positions',
        'market_get_sentiment', 'market_get_overview', 'scanner_get_screen',
        'report_get_compact', 'quant_get_correlations', 'get_guide',
    ],
}

# ── Lazy loading: keywords that suggest tools might be needed ──

_TOOL_HINT_KEYWORDS = (
    'check', 'show', 'get', 'fetch', 'look up', 'compare', 'find',
    'create', 'set', 'add', 'delete', 'remove', 'log', 'open',
    'what is', 'what are', 'how is', 'how are', 'list', 'review',
    'how do i', 'how to', 'where', 'help me', 'guide', 'explain',
    'analyze', 'analyse', 'assess', 'evaluate', 'my plays', 'my play',
    'my positions', 'my position', 'my alerts', 'my trades',
)


def should_include_tools(message: str) -> bool:
    """Lazy check: only include tools when message looks like it needs data."""
    msg_lower = message.lower()
    return any(kw in msg_lower for kw in _TOOL_HINT_KEYWORDS)


def get_tools_for_focus(focus: str, provider: str = 'anthropic',
                        user_tier: str = 'free') -> list:
    """Return LLM-formatted tool definitions for the given focus.

    Filter by user tier so the LLM never sees tools the user cannot
    execute. Without this, Copilot could "discover" Premium-only tools on
    behalf of a Free/Member user and trigger execute_tool which now also gates.

    Args:
        focus: Copilot focus area (chart, journal, plays, etc.)
        provider: 'anthropic' or 'openai' — determines output format.
        user_tier: Caller's tier — used to filter out tools they can't invoke.

    Returns:
        List of tool dicts ready for the LLM API call.
    """
    from carrac.mcp.registry import TOOL_REGISTRY, tool_entitled
    # Ensure tools are loaded
    from carrac.mcp.protocol import _ensure_tools_loaded
    _ensure_tools_loaded()

    tool_names = FOCUS_TOOLS.get(focus, FOCUS_TOOLS['dashboard'])
    tools = []

    for name in tool_names:
        reg = TOOL_REGISTRY.get(name)
        if not reg:
            continue

        # Skip tools the caller's tier can't meet.
        # tool_entitled() honors the tool's `feature` knob (e.g.
        # 'quant_engine'/'backtester') when set, instead of the static
        # min_tier rank — must mirror protocol._handle_tools_call's gate or
        # Copilot's tool list and its actual execute_tool gate below diverge.
        if not tool_entitled(user_tier, reg.get('min_tier', 'free'), reg.get('feature')):
            continue

        if provider == 'anthropic':
            tools.append({
                'name': reg['name'],
                'description': reg['description'],
                'input_schema': reg['inputSchema'],
            })
        else:
            # OpenAI-compatible format (Grok, Together, etc.)
            tools.append({
                'type': 'function',
                'function': {
                    'name': reg['name'],
                    'description': reg['description'],
                    'parameters': reg['inputSchema'],
                },
            })

    return tools


def is_write_tool(name: str) -> bool:
    """Check if a tool requires user confirmation before execution."""
    return name in WRITE_TOOLS


def execute_tool(name: str, arguments: dict, user_id: int,
                 user_tier: str = 'free') -> str:
    """Execute a tool handler and return JSON result string.

    Sets the MCP caller contextvar (user_id, tier) for tool handlers that
    read user identity via get_mcp_caller_user_id / get_mcp_caller_tier.
    Enforces tool_entitled(user_tier, min_tier, feature) before
    call_tool so Copilot cannot smuggle a paid tool call for a lower tier
    (this also honors a tool's `feature` knob, e.g.
    'quant_engine'/'backtester', when set — see registry.tool_entitled).

    Truncates result to 2000 chars to stay within token budget.

    Safe to call from asyncio.to_thread (FastAPI SSE path):
    asyncio.to_thread propagates a *copy* of the calling context to the
    thread, so ContextVar.set() here is thread-local and never bleeds into
    other threads or requests. No Flask app context is pushed — flask.g is
    intentionally NOT used here to avoid the thread-reuse data-exposure bug
    where stale g.api_key_user_id from User A would be readable on the same
    thread when User B's request lands next.
    """
    from carrac.mcp.registry import call_tool, get_tool_min_tier, get_tool_feature, tool_entitled
    from carrac.mcp.protocol import _ensure_tools_loaded
    from carrac.mcp.runtime import set_mcp_caller, reset_mcp_caller

    _ensure_tools_loaded()
    set_mcp_caller(user_id, user_tier)
    try:
        try:
            min_tier = get_tool_min_tier(name)
            feature = get_tool_feature(name)
        except KeyError:
            return json.dumps({'error': f'Unknown tool: {name}'})

        if not tool_entitled(user_tier, min_tier, feature):
            logger.info(
                f"Copilot tool {name} requires {min_tier}; user tier {user_tier!r} denied"
            )
            return json.dumps({
                'error': (
                    f'Tool {name!r} requires the {min_tier} tier. '
                    f'Your current tier {user_tier!r} does not cover it. '
                    f'Upgrade to unlock this capability.'
                ),
                'upgrade_required': True,
                'required_tier': min_tier,
            })

        try:
            result = call_tool(name, arguments)
            text = json.dumps(result, default=str)
            if len(text) > 2000:
                text = text[:1997] + '...'
            return text
        except KeyError:
            return json.dumps({'error': f'Unknown tool: {name}'})
        except Exception as e:
            logger.warning(f"Tool {name} error: {e}")
            return json.dumps({'error': str(e)})
    finally:
        # Defence-in-depth: clear the contextvar within this thread's copied
        # context so nothing downstream in the same callchain can read it.
        reset_mcp_caller()
