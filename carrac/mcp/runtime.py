"""Runtime-agnostic per-request MCP user/tier context.

Lets the MCP dispatcher read the authenticated caller's user_id
and tier without depending on Flask's ``g`` proxy, so the same
``protocol.dispatch()`` and ``tools/*`` modules work from both the MCP
FastAPI router (``carrac/fastapi_app/routers/mcp.py``) and the Copilot
tool-execution path (``carrac/ai/copilot_tools.execute_tool``).

How callers integrate:

* MCP FastAPI handler (``routers/mcp.py``) calls :func:`set_mcp_caller`
  after auth resolution, wraps dispatch in ``asyncio.to_thread``, and
  calls :func:`reset_mcp_caller` in the ``finally`` block.  ContextVars
  are propagated into the thread via ``asyncio.to_thread``'s
  ``copy_context()`` mechanism and mutations in the thread do not escape
  back to the event loop.

* Copilot ``execute_tool`` (``carrac/ai/copilot_tools.py``) also calls
  :func:`set_mcp_caller` / :func:`reset_mcp_caller` around the tool
  invocation.  This function is invoked via ``asyncio.to_thread``, so the
  same isolation guarantee applies.

* Tool handlers and ``protocol._handle_tools_call`` read via
  :func:`get_mcp_caller_user_id` / :func:`get_mcp_caller_tier` /
  :func:`get_mcp_caller_scope`.

NOTE: There is no Flask MCP blueprint, and the getter functions have no
``flask.g`` fallback, which would be a thread-local leakage vector — setting
``flask.g.*`` from a pooled thread would leave a stale user identity
readable by the next request that reused that thread.

Tier default: ``'admin'`` (the shared admin API key has no per-user
binding and inherits the admin tier for all non-user-scoped tools).

Scope: ``None`` means no restriction (admin/internal/copilot callers).
``'read-only'`` means the key was issued with read-only scope and must
not be allowed to call mutating MCP tools (mirrors Flask-side
``_enforce_api_key_scope`` in ``carrac/__init__.py``).
"""

from __future__ import annotations

import contextvars
import hashlib
import hmac
import logging
import threading
from typing import Optional

logger = logging.getLogger('carrac.mcp')

# user_id is None for the shared admin key (no per-user binding).
_user_id_var: contextvars.ContextVar[Optional[int]] = contextvars.ContextVar(
    "carrac_mcp_user_id", default=None
)
_tier_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "carrac_mcp_tier", default=None
)
# scope is None for unrestricted callers (admin key, copilot, internal).
# 'read-only' means the API key was issued read-only and must not invoke
# mutating tools.
_scope_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "carrac_mcp_scope", default=None
)
# True excludes this caller from MCP per-key rate limiting,
# usage analytics, and the weekly usage review's LLM-spend attribution. False for every
# caller except the one explicitly flagged in the DB.
_internal_var: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "carrac_mcp_internal", default=False
)


def set_mcp_caller(
    user_id: Optional[int],
    tier: Optional[str],
    scope: Optional[str] = None,
    is_internal: bool = False,
) -> None:
    """Set the per-request caller for code paths that read via the helpers
    below. Idempotent within a single request.

    ``scope`` should be ``'read-only'`` for read-only API keys, ``None``
    (default) for full-access keys, the shared admin key, and internal
    (copilot) callers. ``is_internal`` is the metering-exclusion flag for automated clients —
    unrelated to ``scope``.
    """
    _user_id_var.set(user_id)
    _tier_var.set(tier)
    _scope_var.set(scope)
    _internal_var.set(is_internal)


def reset_mcp_caller() -> None:
    """Clear the per-request caller. Safe to call without a prior set."""
    _user_id_var.set(None)
    _tier_var.set(None)
    _scope_var.set(None)
    _internal_var.set(False)


def get_mcp_caller_user_id() -> Optional[int]:
    """Read user_id from the contextvar.

    Returns ``None`` for the shared admin key (no per-user binding) or
    when no caller has been set (e.g. outside a request context in tests).
    """
    return _user_id_var.get()


def get_mcp_caller_tier() -> str:
    """Read tier from the contextvar.

    Defaults to ``'admin'`` when no caller has been set, matching the
    shared admin key behaviour for non-user-scoped tools.
    """
    return _tier_var.get() or "free"


def get_mcp_caller_scope() -> Optional[str]:
    """Read the API-key scope from the contextvar.

    Returns ``None`` for unrestricted callers (admin key, copilot,
    tests with no explicit scope set) or ``'read-only'`` for keys that
    were issued with read-only scope.
    """
    return _scope_var.get()


def get_mcp_caller_is_internal() -> bool:
    """Read the metering-exclusion flag for automated clients from the contextvar.

    Returns False for every caller except the one explicitly flagged
    is_internal in the DB (spec §12/§20).
    """
    return _internal_var.get()


# ── request-intent telemetry ────────────────────────
#
# call_tool() in registry.py calls log_intent_dispatch() around every tool
# handler invocation. Everything below is best-effort: a failure here must
# NEVER raise back into call_tool (that would fail the tool call for a
# telemetry problem), so every public entry point is wrapped and swallows
# its own exceptions, logging instead.

INTENT_LOG_RETENTION_DAYS = 90

# Rows carrying a "collection" under one of these conventional keys are
# considered empty when that collection is empty; a bare list/dict/None/""
# falls back to plain Python falsiness. Shallow and tool-shape-agnostic by
# design — good enough for telemetry, not a correctness guarantee.
_COLLECTION_KEYS = (
    'results', 'items', 'plays', 'trades', 'data', 'alerts',
    'drawings', 'rows', 'assets', 'signals', 'sections',
)

_PREV_TOOL_KEY_PREFIX = 'mcd:mcp:prev_tool:'
_PREV_TOOL_TTL_SECONDS = 3600

# Per-process fallback for "previous tool called by this caller" when Redis
# is unavailable. Best-effort only (doesn't survive a restart, isn't shared
# across workers) — the Redis path is what actually keeps this correct in
# production; this just keeps single-worker/test/dev environments useful.
_prev_tool_fallback: dict = {}
_prev_tool_fallback_lock = threading.Lock()


def _hash_user_ref(user_id: Optional[int]) -> Optional[str]:
    """Pseudonymise a user_id for the mcp_intent_log `user_ref` column.

    HMAC-SHA256 keyed on SECRET_KEY, not a bare hash — the user_id space is
    small enough that hashlib.sha256(user_id) alone would be brute-forceable
    in milliseconds, which would defeat the point of pseudonymising it.
    Returns None for the shared admin key (user_id is None — no per-user
    identity to pseudonymise; `tier` already records that this was the
    shared/admin caller).
    """
    if user_id is None:
        return None
    try:
        from carrac.config import Config
        secret = (Config.SECRET_KEY or 'carrac-mcp-intent-log').encode('utf-8')
    except Exception:
        secret = b'carrac-mcp-intent-log'
    digest = hmac.new(
        secret, f'mcp-intent-user:{user_id}'.encode('utf-8'), hashlib.sha256
    ).hexdigest()
    return digest[:32]


def _swap_prev_tool(rate_key: str, tool_name: str) -> Optional[str]:
    """Atomically read-then-set "last tool called" for one caller key.

    Returns the previous tool name, or None on that caller's first call.
    Redis-backed (GETSET — shared across workers, matches the same
    `rate_key` shape used by carrac.mcp.rate_limit) with an in-process dict
    fallback when Redis is unavailable. Never raises.
    """
    key = _PREV_TOOL_KEY_PREFIX + rate_key
    try:
        from carrac.redis_cache import get_redis
        r = get_redis()
        if r is not None:
            prev = r.getset(key, tool_name)
            r.expire(key, _PREV_TOOL_TTL_SECONDS)
            if prev is None:
                return None
            return prev.decode('utf-8') if isinstance(prev, bytes) else prev
    except Exception:
        logger.debug("mcp_intent_log prev_tool Redis lookup failed", exc_info=True)

    with _prev_tool_fallback_lock:
        prev = _prev_tool_fallback.get(key)
        _prev_tool_fallback[key] = tool_name
        return prev


def _is_empty_result(result) -> bool:
    """Best-effort "did this call come back empty" heuristic. See
    _COLLECTION_KEYS docstring above for the trade-off this makes."""
    if result is None:
        return True
    if isinstance(result, (list, tuple, str)):
        return len(result) == 0
    if isinstance(result, dict):
        for key in _COLLECTION_KEYS:
            if key in result:
                val = result[key]
                if isinstance(val, (list, tuple, dict, str)):
                    return len(val) == 0
        return len(result) == 0
    return not result


def log_intent_dispatch(*, tool, arguments, ok, result, elapsed_ms, error_kind,
                        error_detail=None) -> None:
    """Write one mcp_intent_log row for a completed tools/call dispatch.

    Called from registry.call_tool() around every handler invocation.
    MUST NEVER raise — wrapped end-to-end so a telemetry failure can never
    fail (or meaningfully slow down) the tool call it's describing.

    Columns: ts, tool, tier, user_ref (hashed — never the raw user_id or
    email), user_intent, unmet_need, latency_ms, ok, result_empty,
    error_kind, error_detail (the exception MESSAGE, not just its
    class; `error_kind` alone could not distinguish which of a tool's several
    `raise ToolError(...)` lines fired), prev_tool (the previous tool called by
    the same caller, for sequence reconstruction).
    """
    try:
        user_id = get_mcp_caller_user_id()
        tier = get_mcp_caller_tier()
        user_ref = _hash_user_ref(user_id)
        # Mirrors carrac.mcp.rate_limit's per-caller key shape.
        rate_key = f'u{user_id}' if user_id else 'shared'
        prev_tool = _swap_prev_tool(rate_key, tool)

        user_intent = None
        unmet_need = None
        if isinstance(arguments, dict):
            ui = arguments.get('user_intent')
            if isinstance(ui, str) and ui:
                user_intent = ui[:2048]
            un = arguments.get('unmet_need')
            if isinstance(un, str) and un:
                unmet_need = un[:512]

        result_empty = _is_empty_result(result) if ok else False

        from carrac.db_pool import adapt_query, get_conn, get_db_type

        with get_conn() as conn:
            if get_db_type() == 'postgresql':
                ts_expr = 'NOW()'
            else:
                ts_expr = 'CURRENT_TIMESTAMP'
            conn.execute(
                adapt_query(
                    f"""
                    INSERT INTO mcp_intent_log
                        (ts, tool, tier, user_ref, user_intent, unmet_need,
                         latency_ms, ok, result_empty, error_kind, prev_tool,
                         error_detail)
                    VALUES ({ts_expr}, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """
                ),
                (
                    tool, tier, user_ref, user_intent, unmet_need,
                    round(elapsed_ms, 2), ok, result_empty, error_kind, prev_tool,
                    error_detail,
                ),
            )
    except Exception:
        logger.exception(
            "mcp_intent_log write failed for tool=%s (tool call unaffected)", tool
        )


def purge_old_intent_log(retention_days: int = INTENT_LOG_RETENTION_DAYS) -> int:
    """Delete mcp_intent_log rows older than `retention_days`. Returns the
    number of rows removed (0 on any failure — never raises).

    Called nightly by the worker's scheduler (see worker.py's
    intent-log-purge scheduler). A failed purge just means
    yesterday's cleanup slips to the next scheduler wake — it must not take
    down the worker process.
    """
    try:
        from carrac.db_pool import adapt_query, get_conn, get_db_type

        with get_conn() as conn:
            if get_db_type() == 'postgresql':
                cur = conn.execute(
                    "DELETE FROM mcp_intent_log "
                    "WHERE ts < NOW() - (%s || ' days')::interval",
                    (str(int(retention_days)),),
                )
            else:
                cur = conn.execute(
                    adapt_query(
                        "DELETE FROM mcp_intent_log WHERE ts < datetime('now', ?)"
                    ),
                    (f'-{int(retention_days)} days',),
                )
            deleted = cur.rowcount if cur.rowcount is not None else 0
        logger.info(
            "mcp_intent_log purge: removed %s row(s) older than %sd",
            deleted, retention_days,
        )
        return deleted
    except Exception:
        logger.exception("mcp_intent_log purge failed (will retry next scheduler wake)")
        return 0


__all__ = [
    "set_mcp_caller",
    "reset_mcp_caller",
    "get_mcp_caller_user_id",
    "get_mcp_caller_tier",
    "get_mcp_caller_scope",
    "get_mcp_caller_is_internal",
    "log_intent_dispatch",
    "purge_old_intent_log",
    "INTENT_LOG_RETENTION_DAYS",
]
