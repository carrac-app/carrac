"""
MCP JSON-RPC 2.0 protocol handler.

Implements MCP Streamable HTTP methods:
  - initialize
  - tools/list
  - tools/call
  - notifications/* (no response)

JSON-RPC 2.0 error codes:
  -32700  Parse error
  -32600  Invalid Request
  -32601  Method not found
  -32602  Invalid params
  -32603  Internal error
"""

from carrac.site_url import site_base_url
import json
import logging
import time

logger = logging.getLogger('carrac.mcp')

# ── Read-only scope enforcement ────────────────────────────────
# Tools that create, modify, or delete state. A caller whose API key was
# issued with scope='read-only' must NOT be able to invoke any of these.
# Maintained here (one place) rather than as per-tool metadata so the
# enforcement path stays easy to audit and the registry stays clean.
#
# This set uses CANONICAL (post-rename) names only. Callers
# using a pre-rename name are canonicalized by registry.resolve_tool_name()
# in _handle_tools_call BEFORE this check runs — see the call below. If a
# mutating tool's new name is missing from this set, a read-only API key
# silently gains write access under the new name. When adding a tool here,
# also check registry.LEGACY_TOOL_ALIASES: every OLD name that resolves to
# a NEW name in this set must itself have been mutating before the rename
# (verified by tests/test_mcp_scope.py).
#
# Rule: names with prefixes create_/modify_/update_/delete_/archive_/
# log_/close_/manage_/configure_/link_/save_/generate_/bulk_/ are mutating;
# manage_*/alert_manage/share_manage treated as mutating (they both read and
# write — conservative is correct). share_manage mutates too — it
# publishes/revokes a PUBLIC artifact, the most consequential write there is.
WRITE_TOOLS: frozenset = frozenset({
    'play_create',
    'play_update',
    'play_delete',
    'play_archive',
    'play_archive_bulk',
    'play_create_from_formula_alert',
    'share_manage',
    'trade_log',
    'trade_update',
    'trade_close',
    'trade_delete',
    'trade_get_ai_review',
    'watchlist_manage',
    'alert_manage',
    'notification_configure',
    'drawing_create',
    'drawing_update',
    'drawing_delete',
    'drawing_link_to_play',
    'alert_create_from_drawing',
    'alert_create_regime_watch',
    'drawing_create_group',  # was drawing_create_scenario
    'play_draft_from_triggered_alerts',
    'recipe_manage',
    'profile_manage',
    'backtest_save_run',
    # action-discriminated like alert_manage/share_manage/
    # watchlist_manage — action="list" is harmless but save/delete are not, and
    # the gate is per-tool, not per-action. Listed here so a read-only key
    # cannot reach the write actions; read-only callers lose the list action
    # too, which matches how every other *_manage tool is treated.
    'backtest_manage_strategies',
    'report_generate_conditional',
})

# MCP spec revision advertised. '2025-11-25' is the current stable spec; the
# later 2026-07-28 revision replaces the transport with a stateless,
# handshake-free model (a rewrite, not a version bump), so it is NOT adopted.
# 2025-11-25 keeps the initialize-handshake lifecycle our initialize/
# tools/prompts/resources handlers implement, and the capabilities shape we
# advertise (tools/prompts/resources with listChanged/subscribe sub-flags) is
# identical across every revision from 2024-11-05 through 2025-11-25.
PROTOCOL_VERSION = '2025-11-25'
SERVER_NAME = 'carrac'
_server_version = None
_tools_loaded = False


# ── JSON-RPC helpers ───────────────────────────────────────────

def _error_response(req_id, code, message, data=None):
    err = {'code': code, 'message': message}
    if data is not None:
        err['data'] = data
    return {'jsonrpc': '2.0', 'id': req_id, 'error': err}


def _success_response(req_id, result):
    return {'jsonrpc': '2.0', 'id': req_id, 'result': result}


# ── Exceptions ─────────────────────────────────────────────────

class InvalidParams(Exception):
    pass


class ToolError(Exception):
    pass


# ── Lazy tool loading ──────────────────────────────────────────

def _ensure_tools_loaded():
    global _tools_loaded
    if not _tools_loaded:
        import carrac.mcp.tools  # noqa: F401
        import carrac.mcp.primitives  # noqa: F401  — registers prompts + resources
        _tools_loaded = True


# ── Main dispatcher ────────────────────────────────────────────

def dispatch(body):
    """Dispatch a single JSON-RPC 2.0 request. Returns dict or None."""
    if not isinstance(body, dict):
        return _error_response(None, -32600, 'Invalid Request: expected JSON object')

    if body.get('jsonrpc') != '2.0':
        return _error_response(body.get('id'), -32600,
                               'Invalid Request: jsonrpc must be "2.0"')

    method = body.get('method')
    if not isinstance(method, str):
        return _error_response(body.get('id'), -32600,
                               'Invalid Request: method must be a string')

    req_id = body.get('id')
    params = body.get('params', {})

    # Notifications (no id → no response)
    if req_id is None:
        _handle_notification(method, params)
        return None

    handler = _METHOD_HANDLERS.get(method)
    if handler is None:
        return _error_response(req_id, -32601, f'Method not found: {method}')

    try:
        result = handler(params)
        return _success_response(req_id, result)
    except InvalidParams as e:
        return _error_response(req_id, -32602, str(e))
    except ToolError as e:
        return _success_response(req_id, {
            'content': [{'type': 'text', 'text': json.dumps({'error': str(e)})}],
            'isError': True,
        })
    except Exception:
        logger.exception("MCP internal error in method %s", method)
        return _error_response(req_id, -32603, 'Internal error')


# ── Method handlers ────────────────────────────────────────────

def _handle_initialize(params):
    global _server_version
    if _server_version is None:
        try:
            from changelog import APP_VERSION
            _server_version = APP_VERSION
        except Exception:
            _server_version = '0.0.0'

    logger.info("MCP initialize from %s (protocol %s)",
                params.get('clientInfo', {}).get('name', 'unknown'),
                params.get('protocolVersion', ''))

    return {
        'protocolVersion': PROTOCOL_VERSION,
        'capabilities': {
            'tools': {'listChanged': False},
            'prompts': {'listChanged': False},
            'resources': {'listChanged': False, 'subscribe': False},
        },
        'serverInfo': {
            'name': SERVER_NAME,
            'version': _server_version,
        },
        'instructions': (
            'Carrac is a market-data + trading-analysis platform. Query crypto/stock '
            'market data, manage trading plays/drawings/journal, and run quantitative '
            'analysis — all real-time or near-real-time from Binance, Hyperliquid, FRED, '
            'and other sources.\n\n'
            'READ THESE RESOURCES FIRST (resources/list) — they carry the vocabulary + '
            'rules you need to interpret Carrac data correctly:\n'
            '• carrac://glossary/indicators — Consensio, EMA Ribbon, CPR, Structure, '
            'Bollinger Bands, ADX, Volume, Weekly RSI, Key Levels.\n'
            '• carrac://glossary/quant-engine — z-scores, FDR-q correlations, lead-lag '
            'projected-move + CI, 2-state HMM regime, anomaly clusters, transmission chains.\n'
            '• carrac://glossary/backtest-models — the THREE distinct models; never compare '
            'a Hold-N hit rate against a Full-Sim win rate (they measure different things).\n'
            '• carrac://grounding/rules — hard anti-hallucination rules + the non-advice '
            'posture: never write "buy"/"sell"/"you should"/"I recommend"/"I would"; these '
            'data are information to evaluate, not personal financial advice.\n'
            '• carrac://facts/current — live ground-truth macro facts (date, Fed funds + '
            'target, FOMC dates, yields, CPI, DXY, BTC, SPX). Use these instead of training '
            'memory for any macro value.\n\n'
            'PROMPTS (prompts/list) give you structured Carrac workflows: validate-trade, '
            'design-trade, analyze-asset.\n\n'
            'Ground every claim in tool results or the facts resource — if a number is not '
            'in the data, say so rather than inventing it.'
        ),
    }


def _handle_tools_list(params):
    _ensure_tools_loaded()
    from .registry import get_tool_definitions
    from .runtime import get_mcp_caller_tier
    # Filter the advertised tool set by the caller's tier so free users only
    # see tools they can actually call. The router authenticates + sets the
    # caller before dispatch; an unset caller (internal/test) → None → all tools.
    return {'tools': get_tool_definitions(caller_tier=get_mcp_caller_tier())}


def _handle_tools_call(params):
    _ensure_tools_loaded()

    name = params.get('name')
    if not isinstance(name, str):
        raise InvalidParams('tools/call requires params.name (string)')

    arguments = params.get('arguments', {})
    if not isinstance(arguments, dict):
        raise InvalidParams('tools/call params.arguments must be an object')

    from .registry import (
        call_tool, get_tool_min_tier, get_tool_feature, tool_entitled, resolve_tool_name,
    )
    from .runtime import get_mcp_caller_tier, get_mcp_caller_user_id, get_mcp_caller_scope

    # Canonicalize a pre-rename tool name ONCE, up front, so every
    # gate below (tier, feature, read-only scope) and call_tool() itself agree
    # on the same identity. `name` is kept as the caller's original spelling
    # for logging only — see registry.LEGACY_TOOL_ALIASES for the map and
    # registry.LEGACY_ARG_DEFAULTS for the handful of consolidated tools that
    # also need a discriminator (action/entity_type/family) the pre-rename
    # call never sent.
    canonical_name = resolve_tool_name(name)

    # Tier gate — shared admin key (no api_key_tier on g) inherits admin.
    # tool_entitled() checks the tool's `feature` knob (e.g.
    # 'quant_engine'/'backtester') against TIER_LIMITS when set, falling back
    # to the plain min_tier rank otherwise — see registry.tool_entitled.
    try:
        min_tier = get_tool_min_tier(canonical_name)
        feature = get_tool_feature(canonical_name)
    except KeyError:
        raise InvalidParams(f'Unknown tool: {name}')

    caller_tier = get_mcp_caller_tier()
    if not tool_entitled(caller_tier, min_tier, feature):
        logger.warning(
            "MCP tier denial: tool=%s caller_tier=%s min_tier=%s user_id=%s",
            name, caller_tier, min_tier, get_mcp_caller_user_id(),
        )
        raise ToolError(
            f'Tool {name!r} requires {min_tier} tier (current: {caller_tier}). '
            f'This needs a higher plan. An admin can change plans under Admin → Users.'
        )

    # Read-only scope gate — only enforced when scope is explicitly 'read-only'.
    # The shared admin key (user_id=None, tier='admin', scope=None) is NOT
    # restricted; copilot/internal callers (scope=None) are also unaffected.
    caller_scope = get_mcp_caller_scope()
    if caller_scope == 'read-only' and canonical_name in WRITE_TOOLS:
        logger.warning(
            "MCP read-only scope denial: tool=%s user_id=%s",
            name, get_mcp_caller_user_id(),
        )
        raise ToolError(
            f'Tool {name!r} performs write operations and cannot be called '
            f'with a read-only API key. Issue a full-access key at '
            f'{site_base_url()}/settings/api-keys'
        )

    start = time.monotonic()
    try:
        result = call_tool(name, arguments)
    except KeyError:
        raise InvalidParams(f'Unknown tool: {name}')

    elapsed_ms = (time.monotonic() - start) * 1000
    logger.info("MCP tool %s → %.0fms", name, elapsed_ms)

    text = json.dumps(result, default=_json_default)
    return {
        'content': [{'type': 'text', 'text': text}],
    }


def _handle_prompts_list(params):
    _ensure_tools_loaded()
    from .registry import get_prompt_definitions
    from .runtime import get_mcp_caller_tier
    return {'prompts': get_prompt_definitions(caller_tier=get_mcp_caller_tier())}


def _handle_prompts_get(params):
    _ensure_tools_loaded()
    name = params.get('name')
    if not isinstance(name, str):
        raise InvalidParams('prompts/get requires params.name (string)')
    arguments = params.get('arguments', {})
    if not isinstance(arguments, dict):
        raise InvalidParams('prompts/get params.arguments must be an object')

    from .registry import call_prompt, get_prompt_min_tier, tier_meets, PROMPT_REGISTRY
    from .runtime import get_mcp_caller_tier

    try:
        min_tier = get_prompt_min_tier(name)
    except KeyError:
        raise InvalidParams(f'Unknown prompt: {name}')

    caller_tier = get_mcp_caller_tier()
    if not tier_meets(caller_tier, min_tier):
        raise ToolError(
            f'Prompt {name!r} requires {min_tier} tier (current: {caller_tier}). '
            f'This needs a higher plan. An admin can change plans under Admin → Users.'
        )

    messages = call_prompt(name, arguments)
    return {'description': PROMPT_REGISTRY[name]['description'], 'messages': messages}


def _handle_resources_list(params):
    _ensure_tools_loaded()
    from .registry import get_resource_definitions
    from .runtime import get_mcp_caller_tier
    return {'resources': get_resource_definitions(caller_tier=get_mcp_caller_tier())}


def _handle_resources_read(params):
    _ensure_tools_loaded()
    uri = params.get('uri')
    if not isinstance(uri, str):
        raise InvalidParams('resources/read requires params.uri (string)')

    from .registry import read_resource, get_resource_min_tier, tier_meets, RESOURCE_REGISTRY
    from .runtime import get_mcp_caller_tier

    try:
        min_tier = get_resource_min_tier(uri)
    except KeyError:
        raise InvalidParams(f'Unknown resource: {uri}')

    caller_tier = get_mcp_caller_tier()
    if not tier_meets(caller_tier, min_tier):
        raise ToolError(
            f'Resource {uri!r} requires {min_tier} tier (current: {caller_tier}). '
            f'This needs a higher plan. An admin can change plans under Admin → Users.'
        )

    text = read_resource(uri)
    return {'contents': [{
        'uri': uri,
        'mimeType': RESOURCE_REGISTRY[uri]['mimeType'],
        'text': text,
    }]}


def _handle_notification(method, params):
    if method == 'notifications/initialized':
        logger.info("MCP client initialized")
    else:
        logger.debug("MCP notification: %s", method)


def _json_default(obj):
    """JSON serializer for numpy/datetime types."""
    import numpy as np
    from datetime import datetime, date
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        return float(obj)
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    raise TypeError(f"Not JSON serializable: {type(obj)}")


_METHOD_HANDLERS = {
    'initialize': _handle_initialize,
    'tools/list': _handle_tools_list,
    'tools/call': _handle_tools_call,
    'prompts/list': _handle_prompts_list,
    'prompts/get': _handle_prompts_get,
    'resources/list': _handle_resources_list,
    'resources/read': _handle_resources_read,
}
