"""
MCP tool registry — @mcp_tool decorator and TOOL_REGISTRY.

Each tool is registered with name, description, inputSchema (JSON Schema),
a handler function, and an explicit `min_tier` ('free' | 'member' | 'premium' | 'admin').
The registry is populated at import time when tools/*.py modules are imported.

min_tier MUST be supplied explicitly. Omitting it triggers a warning
at registration time (not a hard error so existing deployments don't crash, but
CI should treat these warnings as failures).
"""

import logging
import time

logger = logging.getLogger('carrac.mcp')

# Request-intent telemetry schema fields, injected into every
# tool's inputSchema by the @mcp_tool decorator below — NOT by editing each
# individual tool definition (the tool set was reduced by the domain_verb
# rename + alerts/backtest/share consolidation). See mcp_tool()'s docstring
# for the field semantics; call_tool() is where the resulting arguments get
# logged.
_USER_INTENT_PROPERTY = {
    'type': 'string',
    'maxLength': 2048,
    'description': (
        "One-sentence paraphrase of the user's request that led to this tool "
        "call. Include when you have conversational context."
    ),
}
_UNMET_NEED_PROPERTY = {
    'type': 'string',
    'maxLength': 512,
    'description': (
        'If you looked for a capability that does not exist, name it in one line.'
    ),
}

# The ~10 entry-point tools that also get `unmet_need` — the free-text
# feature-request channel. Kept as one explicit set here (not scattered
# per-tool flags) so the "which tools get this" question has one answer.
ENTRY_POINT_INTENT_TOOLS = frozenset({
    'get_started',
    'get_guide',
    'scanner_get_screen',
    'market_get_overview',
    'play_list',
    'report_get_compact',
    'quant_get_data',
    'trade_list',
    'report_get_section',
    'market_search_symbol',
})

# {tool_name: ToolDef} — populated by @mcp_tool decorator
TOOL_REGISTRY = {}

# Tier ordering — higher = more privileged.
# 'tester' == 'premium' for entitlement checks (admin-granted comp
# tier that mirrors Premium access). Without this, tier_meets('tester','premium')
# was False and every tester user was silently blocked from Premium MCP tools.
TIER_RANK = {'free': 0, 'member': 1, 'tester': 2, 'premium': 2, 'admin': 3}

# Sentinel distinguishing "min_tier not supplied" from an explicit value.
_UNSET = object()


def tier_meets(caller_tier, min_tier):
    return TIER_RANK.get(caller_tier or 'free', 0) >= TIER_RANK.get(min_tier or 'free', 0)


def tool_entitled(caller_tier, min_tier, feature=None):
    """True if ``caller_tier`` may use a registry entry (tool/prompt/resource).

    Entries registered with a ``feature`` knob (e.g.
    'quant_engine', 'backtester', 'backtester_save') are gated by that
    TIER_LIMITS boolean — the same knob the Flask ``tier_feature_required``
    decorator checks — instead of the static ``min_tier`` rank. A hardcoded
    ``min_tier`` silently desyncs from the tier table: if TIER_LIMITS flips
    'quant_engine'/'backtester' to True for 'member', a hardcoded
    ``min_tier='premium'`` on the tools never moves. Routing through the live knob means the next tier
    restructure can't repeat that bug for THESE tools without also updating
    TIER_LIMITS (which is the one place that must change anyway).

    Entries without a ``feature`` fall back to the plain min_tier rank check
    (``tier_meets``) — the existing behavior for every other tool.
    """
    if feature:
        from carrac.auth.decorators import get_tier_limits
        return bool(get_tier_limits(caller_tier).get(feature, False))
    return tier_meets(caller_tier, min_tier)


def mcp_tool(name, description, input_schema=None, min_tier=_UNSET, feature=None):
    """Register an MCP tool handler.

    Args:
        name: Unique tool identifier.
        description: Human-readable description for tools/list.
        input_schema: JSON Schema for arguments (optional).
        min_tier: Minimum tier required to invoke — 'free' | 'member' | 'premium' | 'admin'.
                  MUST be supplied explicitly. Omitting logs a WARNING and
                  defaults to 'free' to avoid breaking existing callers. Still used for
                  tools/list discovery messaging and as the informational floor when
                  ``feature`` is set (see below).
        feature: Optional TIER_LIMITS boolean knob name (e.g. 'quant_engine',
                 'backtester', 'backtester_save'). When set, actual entitlement is
                 decided by ``get_tier_limits(caller_tier).get(feature)``
                 rather than the static min_tier rank — so this tool automatically
                 tracks TIER_LIMITS if a future tier restructure changes who gets the
                 knob, without needing another code edit here. ``min_tier`` should
                 still be set to the lowest tier that currently has the knob True
                 (informational + used verbatim in the upgrade-required error message).

    Usage:
        @mcp_tool('market_get_sentiment', 'Get market sentiment data.', {...}, min_tier='member')
        def market_get_sentiment(arguments):
            ...

        @mcp_tool('quant_get_anomalies', '...', {...}, min_tier='member', feature='quant_engine')
        def quant_get_anomalies(arguments):
            ...

    Every tool's inputSchema gets a `user_intent` property injected
    here (never edited per-tool); the ~10 tools in ENTRY_POINT_INTENT_TOOLS
    also get `unmet_need`. Both are optional (not added to `required`) so no
    existing caller breaks. Handlers never need to read these — arguments is
    just a plain dict, extra keys are ignored by every handler's `.get(...)`
    access pattern — call_tool() below is what actually logs them.
    """
    if min_tier is _UNSET:
        logger.warning(
            "[SECURITY] MCP tool %r registered without explicit min_tier — defaulting to 'free'. "
            "Add min_tier=... to the @mcp_tool decorator to silence this warning.",
            name,
        )
        min_tier = 'free'

    if min_tier not in TIER_RANK:
        raise ValueError(f"Invalid min_tier {min_tier!r}; must be one of {list(TIER_RANK)}")

    def decorator(fn):
        if name in TOOL_REGISTRY:
            raise ValueError(f"Duplicate MCP tool name: {name}")

        schema = input_schema or {'type': 'object', 'properties': {}}
        # Copy so we never mutate a dict literal that might be shared/reused,
        # and so `properties` (possibly absent) is safe to add to.
        schema = dict(schema)
        properties = dict(schema.get('properties') or {})
        properties['user_intent'] = _USER_INTENT_PROPERTY
        if name in ENTRY_POINT_INTENT_TOOLS:
            properties['unmet_need'] = _UNMET_NEED_PROPERTY
        schema['properties'] = properties

        TOOL_REGISTRY[name] = {
            'name': name,
            'description': description,
            'inputSchema': schema,
            'handler': fn,
            'min_tier': min_tier,
            'feature': feature,
        }
        return fn
    return decorator


# ── legacy tool-name aliases ──────────────────────────
#
# TEMPORARY. An external strategist client (separate repo, deployed
# independently on its own host) calls these tools over MCP, including
# many model-facing references baked into its prompts. It cannot
# land in the same diff as this rename, and a stale name there is a SILENT
# runtime "tool not found" mid-trading-decision for that LLM — materially
# worse than a red test. So: old names keep dispatching to the new/
# consolidated handler here, but are never added to TOOL_REGISTRY itself —
# get_tool_definitions() only ever iterates canonical entries, so tools/list
# genuinely reports the post-rename count. Remove this whole block (and the
# two dicts below) once that client's prompts are migrated to the new names —
# a planned follow-up.
LEGACY_TOOL_ALIASES = {
    'ai_trade_review': 'trade_get_ai_review',
    'archive_play': 'play_archive',
    'backtest_conditions': 'backtest_get_historical_analog',
    'backtest_indicator_formula': 'backtest_run',
    'backtest_play': 'backtest_run',
    'backtest_strategy': 'backtest_simulate',
    'batch_query': 'util_batch_query',
    'bulk_archive_plays': 'play_archive_bulk',
    'close_trade': 'trade_close',
    'configure_notifications': 'notification_configure',
    'create_drawing': 'drawing_create',
    'create_drawing_alert': 'alert_create_from_drawing',
    'create_formula_play': 'play_create_from_formula_alert',
    'create_play': 'play_create',
    'create_quant_alert': 'alert_manage',
    'create_regime_change_alert': 'alert_create_regime_watch',
    'create_scenario_with_children': 'drawing_create_group',
    'delete_drawing': 'drawing_delete',
    'delete_play': 'play_delete',
    'delete_trade': 'trade_delete',
    # Scenarios are now called Plays; the tool is renamed to
    # describe the mechanism (a parent drawing + linked children) rather
    # than the retired "scenario" concept. Old name kept dispatching here
    # so the Strategy Lab and any published/external caller keep working —
    # same shim pattern as the legacy-alias block above, not a new mechanism.
    'drawing_create_scenario': 'drawing_create_group',
    'evaluate_play': 'play_evaluate',
    'generate_report_on_condition': 'report_generate_conditional',
    'get_analogues': 'quant_get_analogues',
    'get_asset_detail': 'market_get_detail',
    'get_backtest_run': 'backtest_get_runs',
    'get_chain_history': 'quant_get_chain_history',
    'get_chart': 'market_get_chart',
    'get_chart_screenshot': 'chart_get_screenshot',
    'get_confluence_zones': 'scanner_get_confluence_zones',
    'get_correlations': 'quant_get_correlations',
    'get_cot': 'quant_get_cot',
    'get_derivatives_regime': 'quant_get_regime',
    'get_drawings': 'drawing_list',
    'get_dsl_reference': 'indicator_get_dsl_reference',
    'get_economic_calendar': 'market_get_calendar',
    'get_indicator_definition': 'indicator_get_definition',
    'get_leadlag': 'quant_get_leadlag_ranking',
    'get_leadlag_pair': 'quant_get_leadlag_pair',
    'get_live_stream': 'scanner_get_live_stream',
    'get_market_overview': 'market_get_overview',
    'get_morning_report_raw': 'report_get_raw',
    'get_morning_report_section': 'report_get_section',
    'get_plays': 'play_list',
    'get_plays_context': 'play_get_digest',
    'get_positions': 'trade_get_positions',
    'get_quant_anomalies': 'quant_get_anomalies',
    'get_quant_data': 'quant_get_data',
    'get_regime_history': 'quant_get_regime_history',
    'get_report': 'report_get_compact',
    'get_scanner': 'scanner_get_screen',
    'get_scenario_children': 'drawing_get_scenario_children',
    'get_sentiment': 'market_get_sentiment',
    'get_smart_money_positioning': 'onchain_get_smart_money',
    'get_trade_analytics': 'trade_get_analytics',
    'get_transitions': 'scanner_get_transitions',
    'get_transmission_chains': 'quant_get_transmission_chains',
    'get_triggered_alerts': 'alert_list_triggered',
    'get_volume_profile': 'chart_get_volume_profile',
    'link_drawing_to_play': 'drawing_link_to_play',
    'list_backtest_runs': 'backtest_get_runs',
    'list_drawing_alerts': 'alert_manage',
    'list_formula_alerts': 'alert_manage',
    'list_indicators': 'indicator_list',
    'list_quant_alerts': 'alert_manage',
    'list_regime_alerts': 'alert_manage',
    'list_trades': 'trade_list',
    'log_trade': 'trade_log',
    'manage_ai_recipes': 'recipe_manage',
    'manage_alerts': 'alert_manage',
    'manage_trading_profile': 'profile_manage',
    'manage_watchlist': 'watchlist_manage',
    'modify_play': 'play_update',
    'report_diff': 'report_get_diff',
    'resolve_timestamp': 'util_resolve_timestamp',
    'run_custom_formula': 'indicator_run_formula',
    'save_backtest_run': 'backtest_save_run',
    'scan_and_backtest': 'backtest_run',
    'search_symbol': 'market_search_symbol',
    'share_chart': 'share_manage',
    'share_play': 'share_manage',
    'share_trade': 'share_manage',
    'triggered_alerts_to_plays': 'play_draft_from_triggered_alerts',
    'unshare_chart': 'share_manage',
    'unshare_play': 'share_manage',
    'unshare_trade': 'share_manage',
    'update_drawing': 'drawing_update',
    'update_trade': 'trade_update',
}

# A handful of consolidated tools collapsed several distinct OLD tools into
# ONE new tool discriminated by a new required-ish param (action / entity_type
# / family) that simply did not exist before this rename — a bare name swap
# isn't enough to keep these dispatching correctly. These defaults are merged
# UNDER whatever the caller actually sent (so nothing a legacy caller
# genuinely passes is overridden), keyed by the legacy name so the discriminator
# always matches which legacy tool was actually invoked.
#
# backtest_run's own three sources (formula/play/scan) are NOT listed here on
# purpose — backtest_run infers `source` from which arguments are present
# (play_id => play, no symbol => scan, else formula) so old callers of
# backtest_play/backtest_indicator_formula/scan_and_backtest already send
# everything it needs without any forcing. backtest_get_runs is the same
# story: presence/absence of run_id alone disambiguates get vs list.
LEGACY_ARG_DEFAULTS = {
    'share_chart':         {'entity_type': 'chart', 'action': 'share'},
    'unshare_chart':       {'entity_type': 'chart', 'action': 'unshare'},
    'share_play':          {'entity_type': 'play',  'action': 'share'},
    'unshare_play':        {'entity_type': 'play',  'action': 'unshare'},
    'share_trade':         {'entity_type': 'trade', 'action': 'share'},
    'unshare_trade':       {'entity_type': 'trade', 'action': 'unshare'},
    'list_quant_alerts':   {'action': 'list', 'family': 'quant'},
    'list_drawing_alerts': {'action': 'list', 'family': 'drawing'},
    'list_regime_alerts':  {'action': 'list', 'family': 'regime'},
    'list_formula_alerts': {'action': 'list', 'family': 'formula'},
    'create_quant_alert':  {'action': 'create'},
}


def resolve_tool_name(name):
    """Canonicalize a possibly-legacy tool name. Identity for current names."""
    return LEGACY_TOOL_ALIASES.get(name, name)


def get_tool_definitions(caller_tier=None):
    """Return tool definitions for tools/list (without handler refs).

    When ``caller_tier`` is given, only tools the caller is entitled to invoke
    are returned — so free users don't see (or try) the paid tools. Execution
    is independently gated per-call in protocol.py; this filter is for
    discovery/UX. ``caller_tier=None`` (internal/test callers) returns all tools.
    """
    return [
        {
            'name': t['name'],
            'description': t['description'],
            'inputSchema': t['inputSchema'],
        }
        for t in TOOL_REGISTRY.values()
        if caller_tier is None or tool_entitled(caller_tier, t['min_tier'], t.get('feature'))
    ]


def get_tool_min_tier(name):
    tool = TOOL_REGISTRY.get(resolve_tool_name(name))
    if not tool:
        raise KeyError(f"Unknown tool: {name}")
    return tool.get('min_tier', 'free')


def get_tool_feature(name):
    """Return the tool's TIER_LIMITS feature knob name, or None if unset."""
    tool = TOOL_REGISTRY.get(resolve_tool_name(name))
    if not tool:
        raise KeyError(f"Unknown tool: {name}")
    return tool.get('feature')


def _unknown_arguments(tool, arguments) -> list:
    """Argument names the caller sent that the tool does not declare.

    Lenient, NOT strict: this only reports, it never rejects.
    Rejecting would break the LEGACY_ARG_DEFAULTS aliases and any caller that
    has been sending a stray field harmlessly for months. The goal is to stop
    the drop being SILENT, not to start refusing work.

    Returns [] on any doubt — a tool with no declared properties (or a
    malformed schema) must not produce a list of false accusations.
    """
    try:
        props = ((tool.get('inputSchema') or {}).get('properties')) or {}
        if not props or not isinstance(arguments, dict):
            return []
        return sorted(k for k in arguments if k not in props)
    except Exception:
        return []


def call_tool(name, arguments):
    """Execute a tool handler. Raises KeyError if not found.

    This is the single chokepoint every tools/call dispatch
    passes through (protocol._handle_tools_call calls it directly), so it's
    where the mcp_intent_log row gets written — one row per handler
    invocation, never for an unknown-tool KeyError (that's a routing error,
    not a dispatch). Behavior for the caller is unchanged: the handler's
    return value / exception propagate exactly as before; logging is
    best-effort and can NEVER affect the tool call (see
    runtime.log_intent_dispatch, and the belt-and-suspenders try/except
    here around even calling it).

    ``name`` may be a pre-rename legacy name (see
    LEGACY_TOOL_ALIASES) — resolved to the canonical registry entry here.
    A handful of consolidated tools also get LEGACY_ARG_DEFAULTS merged in
    (keyed by the ORIGINAL name, before resolution) so e.g. a legacy
    share_chart call still carries entity_type='chart'/action='share' even
    though that param doesn't exist under the legacy name. mcp_intent_log below
    still logs the caller's original ``name`` — historical rows keep
    whatever name was actually dispatched, by design.
    """
    canonical = resolve_tool_name(name)
    legacy_defaults = LEGACY_ARG_DEFAULTS.get(name)
    if legacy_defaults:
        arguments = {**legacy_defaults, **arguments}

    tool = TOOL_REGISTRY.get(canonical)
    if not tool:
        raise KeyError(f"Unknown tool: {name}")

    start = time.monotonic()
    ok = True
    error_kind = None
    error_detail = None
    result = None
    try:
        result = tool['handler'](arguments)
        return result
    except Exception as e:
        ok = False
        error_kind = type(e).__name__
        # The CLASS alone is not a diagnosis: a tool that fails repeatedly
        # with `ToolError` on every row leaves no way to tell which of its
        # several `raise ToolError(...)` lines fired without reading the
        # source and guessing, so the message is recorded too.
        # Flattened and truncated: an upstream HTML error page must not
        # become the telemetry.
        error_detail = str(e)[:500].replace('\n', ' ').replace('\r', ' ')
        # And name the arguments we THREW AWAY. Every tool reads
        # its arguments with .get(), and nothing has ever compared them against
        # the tool's own declared inputSchema — so a caller using the wrong
        # field names has them dropped in silence and hears only about whatever
        # required field ended up missing.
        unknown = _unknown_arguments(tool, arguments)
        if unknown:
            error_detail = f"{error_detail} | ignored unknown args: {', '.join(unknown)}"[:800]
        raise
    finally:
        elapsed_ms = (time.monotonic() - start) * 1000.0
        try:
            from carrac.mcp.runtime import log_intent_dispatch
            log_intent_dispatch(
                tool=name,
                arguments=arguments,
                ok=ok,
                result=result,
                elapsed_ms=elapsed_ms,
                error_kind=error_kind,
                error_detail=error_detail,
            )
        except Exception:
            logger.exception(
                "mcp_intent_log dispatch failed for tool=%s (tool call unaffected)",
                name,
            )


# ── Prompts + Resources (MCP primitives beyond tools) ───────────
#
# MCP advertises three primitive kinds: tools (model-invoked actions),
# prompts (user-selectable templates), and resources (readable context).
# Carrac was tools-only; these registries add prompts + resources so the
# "carrac expertise" (glossary, grounding rules, validated workflows) that
# lives in the in-app chat is also available to an external LLM that
# connects via MCP. Both mirror TOOL_REGISTRY's shape + tier-filtering.

PROMPT_REGISTRY = {}     # {name: {name, description, arguments, handler, min_tier}}
RESOURCE_REGISTRY = {}   # {uri:  {uri, name, description, mimeType, handler, min_tier}}


def mcp_prompt(name, description, arguments=None, min_tier='free'):
    """Register an MCP prompt template.

    The decorated ``fn(arguments: dict) -> list[message]`` returns the prompt
    messages, each ``{'role': 'user'|'assistant', 'content': {'type': 'text',
    'text': ...}}``. ``arguments`` is the MCP arg spec: a list of
    ``{'name', 'description', 'required'}`` dicts surfaced in prompts/list.
    """
    if min_tier not in TIER_RANK:
        raise ValueError(f"Invalid min_tier {min_tier!r}; must be one of {list(TIER_RANK)}")

    def decorator(fn):
        if name in PROMPT_REGISTRY:
            raise ValueError(f"Duplicate MCP prompt name: {name}")
        PROMPT_REGISTRY[name] = {
            'name': name,
            'description': description,
            'arguments': arguments or [],
            'handler': fn,
            'min_tier': min_tier,
        }
        return fn
    return decorator


def mcp_resource(uri, name, description, mime_type='text/markdown', min_tier='free'):
    """Register an MCP resource.

    The decorated ``fn() -> str`` returns the resource text, computed fresh on
    every read so dynamic resources (e.g. live market facts) stay current.
    """
    if min_tier not in TIER_RANK:
        raise ValueError(f"Invalid min_tier {min_tier!r}; must be one of {list(TIER_RANK)}")

    def decorator(fn):
        if uri in RESOURCE_REGISTRY:
            raise ValueError(f"Duplicate MCP resource uri: {uri}")
        RESOURCE_REGISTRY[uri] = {
            'uri': uri,
            'name': name,
            'description': description,
            'mimeType': mime_type,
            'handler': fn,
            'min_tier': min_tier,
        }
        return fn
    return decorator


def get_prompt_definitions(caller_tier=None):
    """Prompt specs for prompts/list (without handler refs), tier-filtered."""
    return [
        {'name': p['name'], 'description': p['description'], 'arguments': p['arguments']}
        for p in PROMPT_REGISTRY.values()
        if caller_tier is None or tier_meets(caller_tier, p['min_tier'])
    ]


def get_resource_definitions(caller_tier=None):
    """Resource specs for resources/list (without handler refs), tier-filtered."""
    return [
        {'uri': r['uri'], 'name': r['name'],
         'description': r['description'], 'mimeType': r['mimeType']}
        for r in RESOURCE_REGISTRY.values()
        if caller_tier is None or tier_meets(caller_tier, r['min_tier'])
    ]


def get_prompt_min_tier(name):
    p = PROMPT_REGISTRY.get(name)
    if not p:
        raise KeyError(f"Unknown prompt: {name}")
    return p.get('min_tier', 'free')


def get_resource_min_tier(uri):
    r = RESOURCE_REGISTRY.get(uri)
    if not r:
        raise KeyError(f"Unknown resource: {uri}")
    return r.get('min_tier', 'free')


def call_prompt(name, arguments):
    """Build a prompt's messages. Raises KeyError if not found."""
    p = PROMPT_REGISTRY.get(name)
    if not p:
        raise KeyError(f"Unknown prompt: {name}")
    return p['handler'](arguments or {})


def read_resource(uri):
    """Return a resource's current text. Raises KeyError if not found."""
    r = RESOURCE_REGISTRY.get(uri)
    if not r:
        raise KeyError(f"Unknown resource: {uri}")
    return r['handler']()
