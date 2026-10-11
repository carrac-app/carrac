"""FastAPI MCP router.

Serves ``POST /mcp``: auth by API key in ``X-API-Key`` or
``Authorization: Bearer …``, a per-tier rate limit and a JSON-RPC 2.0
envelope. The dispatcher (``carrac.mcp.protocol.dispatch``) is reached
through the runtime-agnostic caller contextvar in ``carrac/mcp/runtime.py``.

Every dispatch call is sync (DB / Binance / numpy / drawing-DB
workloads), so it is wrapped in ``asyncio.to_thread`` and the event loop
stays unblocked while a tool runs.

Strangler dispatch: ``("/mcp", frozenset({"POST"}))`` lives in
``carrac/asgi.py``'s ``MIGRATED_WRITE_PATHS``.

CSRF: `/mcp` is in `_SKIP_PREFIXES` in csrf_middleware.py — the API key
header is the auth story; no CSRF token is supplied by SDK clients.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse, Response

logger = logging.getLogger("carrac.fastapi.mcp")

router = APIRouter()


def _extract_api_key(
    x_api_key: Optional[str],
    authorization: Optional[str],
    x_internal_key: Optional[str] = None,
) -> tuple[Optional[str], bool]:
    """Pick the API key and say whether it came from the internal header.

    Returns ``(key, from_internal)``. ``X-Internal-Key`` wins (server-to-
    server — the engine and the deploy smoke tests), then ``X-API-Key``,
    then ``Authorization: Bearer <key>``. Only a key from
    ``X-Internal-Key`` may resolve as the shared admin key; before this the
    router read only the public header and handed the shared deployment
    credential admin tier, unmetered.
    """
    if x_internal_key:
        return x_internal_key, True
    if x_api_key:
        return x_api_key, False
    if authorization and authorization.startswith("Bearer "):
        return authorization[7:].strip() or None, False
    return None, False


def _jsonrpc_error(req_id: Any, code: int, message: str, data: Any = None) -> dict:
    err: dict = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": err}


@router.post("/mcp")
async def mcp_endpoint(
    request: Request,
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
    authorization: Optional[str] = Header(default=None),
    x_internal_key: Optional[str] = Header(default=None, alias="X-Internal-Key"),
) -> Response:
    """MCP Streamable HTTP endpoint (JSON-RPC 2.0).

    Behaviour:

    1. Read API key from ``X-API-Key`` or ``Authorization: Bearer …``.
    2. Verify via ``carrac.auth.decorators.verify_api_key`` (sync, in
       a thread). On failure → 401 + JSON-RPC ``-32000``.
    3. Default the caller's tier to ``'admin'`` for the shared key
       (invariant: never default ``user_id`` to 1).
    4. Per-key rate limit via ``carrac.mcp.rate_limit.check_rate_limit``
       (Redis-backed, sliding window). On breach → 429 + JSON-RPC
       ``-32001`` + ``Retry-After`` header.
    5. Parse body as JSON. Pydantic stays out of the way: the dispatcher
       returns its own ``-32700`` / ``-32600`` errors so we don't double-
       validate the envelope shape (FastAPI 422 → 400 conversion would
       lose the JSON-RPC error code).
    6. Wrap ``protocol.dispatch`` in ``asyncio.to_thread`` so sync tool
       handlers don't block the loop.
    7. Return ``JSONResponse(result)`` or 204 for notifications.
    """
    # Sentry tag — request-scoped, doesn't leak across endpoints.
    try:
        import sentry_sdk

        sentry_sdk.set_tag("surface", "mcp")
    except Exception:
        pass

    api_key, from_internal = _extract_api_key(x_api_key, authorization, x_internal_key)
    if not api_key:
        return JSONResponse(
            status_code=401,
            content=_jsonrpc_error(
                None, -32000, "Authentication required. Use header: X-API-Key"
            ),
        )

    # Use the runtime-agnostic verification helper so we don't depend on
    # ``flask.g`` mutations inside the ASGI request scope. Returns
    # ``(ok, user_id, tier, scope, is_internal)`` — for the shared admin key,
    # ``user_id`` and ``tier`` are None (never default ``user_id`` to 1).
    from carrac.auth.decorators import resolve_api_key
    from carrac.mcp.runtime import reset_mcp_caller, set_mcp_caller

    ok, user_id, tier, scope, is_internal = await asyncio.to_thread(
        lambda: resolve_api_key(api_key, allow_shared=from_internal))
    if not ok:
        return JSONResponse(
            status_code=401,
            content=_jsonrpc_error(
                None, -32000, "Authentication required. Use header: X-API-Key"
            ),
        )
    if not tier:
        tier = "admin"  # shared admin key — scope is None (unrestricted)
    set_mcp_caller(user_id, tier, scope, is_internal)

    try:
        # ── Per-key rate limit ───────────────────────────────────────
        from carrac.mcp.rate_limit import RateLimitExceeded, check_rate_limit

        rate_key = f"u{user_id}" if user_id else "shared"
        try:
            if not is_internal:
                await asyncio.to_thread(check_rate_limit, rate_key, tier)
        except RateLimitExceeded as e:
            resp = JSONResponse(
                status_code=429,
                content=_jsonrpc_error(
                    None,
                    -32001,
                    str(e),
                    {
                        "limit": e.limit,
                        "window": e.window_label,
                        "retry_after": e.retry_after,
                    },
                ),
            )
            resp.headers["Retry-After"] = str(e.retry_after)
            return resp

        # ── Parse body ───────────────────────────────────────────────
        try:
            body = await request.json()
        except Exception:
            body = None
        if body is None:
            return JSONResponse(
                status_code=400,
                content=_jsonrpc_error(None, -32700, "Parse error: invalid JSON"),
            )

        # ── Dispatch ─────────────────────────────────────────────────
        from carrac.mcp.protocol import dispatch

        result = await asyncio.to_thread(dispatch, body)
        if result is None:
            return Response(status_code=204)
        return JSONResponse(result)
    finally:
        # Don't leak caller into the next ASGI request that happens to
        # land on the same event-loop task. ContextVars in Starlette are
        # per-task already, but explicit reset is defence in depth.
        reset_mcp_caller()
