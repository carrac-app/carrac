"""Shared rate-limit classification — single source of truth for both runtimes.

Cross-cutting infra. Before this module existed, two near-identical
classification implementations lived in:

* ``carrac/rate_pools.py::classify_request()`` — Flask, writes ``flask.g.*``.
* ``carrac/fastapi_app/deps/auth.py::_classify()`` — FastAPI, returns ``RateContext``.

Both call ``_classify_pure`` here and then attach the result to whichever
side-effect bucket their framework requires (``flask.g`` for Flask, the
``RateContext`` dataclass on ``request.state.rate`` for FastAPI). One
algorithm, one place to edit when the pool taxonomy changes.

The helper is framework-free. It accepts a small ``RequestLike`` adapter
plus a session-getter callable; concrete adapters for both runtimes live
in this same module.
"""

from __future__ import annotations

from typing import Callable, Optional, Protocol

from carrac.fastapi_app.deps.auth import RateContext


SessionGetter = Callable[[], dict]


class RequestLike(Protocol):
    """Duck-type satisfied by both Flask and FastAPI request adapters."""

    def header(self, name: str) -> Optional[str]:
        """Case-insensitive header lookup. Returns None if absent."""
        ...

    def query_param(self, name: str) -> Optional[str]:
        """Query-string lookup. Returns None if absent."""
        ...

    @property
    def client_ip(self) -> str:
        """Best-effort client IP. X-Forwarded-For first hop wins."""
        ...


def _classify_pure(req: RequestLike, get_session: SessionGetter) -> RateContext:
    """Classify a request into a rate pool. Pure — no side-effects.

    Order mirrors the original Flask ``rate_pools.classify_request()``: API
    key first (covers MCP, REST API, bots), widget token next, session third,
    anonymous fallback last.

    Identity attrs (``api_key_user_id`` etc.) are populated as a side-effect
    of the api_key/widget paths so the returned ``RateContext`` is the
    single source of truth for both rate-limit bucketing AND identity
    introspection downstream.
    """
    ctx = RateContext(request_id=req.header("x-request-id") or "")

    # 1. API key (header only — URL-param keys are rejected separately).
    # X-Internal-Key is for server-to-server calls; X-API-Key is the public
    # header used by external/SDK callers with per-user cvx_k_ keys.
    #
    # The two headers are deliberately NOT collapsed before the
    # lookup any more. This classifier runs on EVERY request (before_request)
    # and stamps g.rate_tier, which get_request_tier() — and through it every
    # admin gate, the Deribit gate included — reads first. Collapsing the
    # headers here is what made the shared deployment credential a working
    # admin key on the public header, regardless of what verify_api_key did.
    internal_key = req.header("X-Internal-Key")
    api_key = internal_key or req.header("X-API-Key")
    if api_key:
        # Local import: ``pure_resolvers`` already does the same DB+env work
        # but pulls in DB connections; defer until we know we have a key.
        from carrac.auth.pure_resolvers import resolve_api_key

        info = resolve_api_key(api_key, allow_shared=bool(internal_key))
        if info is not None:
            ctx.pool = "api_key"
            ctx.api_key_scope = info.get("scope", "full")
            ctx.api_key_internal = bool(info.get("is_internal", False))
            if info["user_id"] is None:
                # Shared admin key: no user row, blanket admin tier.
                ctx.key = f"api:{api_key[:8]}"
                ctx.tier = "admin"
                ctx.api_key_tier = "admin"
            else:
                ctx.api_key_user_id = info["user_id"]
                ctx.api_key_tier = info.get("tier", "free")
                ctx.tier = ctx.api_key_tier or "free"
                ctx.key = f"api:user:{info['user_id']}"
            return ctx

    # 2. Widget token (Bearer header or ?token= query param).
    auth_header = req.header("authorization") or ""
    token_param = req.query_param("token") or ""
    token_val = ""
    if auth_header.lower().startswith("bearer "):
        token_val = auth_header[7:].strip()
    elif token_param.startswith("cvx_w_"):
        token_val = token_param.strip()
    if token_val.startswith("cvx_w_"):
        ctx.pool = "widget_token"
        ctx.key = f"widget:{token_val[:12]}"
        # Verification + DB lookup is deferred to widget_token_user; we only
        # need the bucket key here. Mirrors Flask's classify_request.
        return ctx

    # 3. Session user.
    sess = get_session() or {}
    user_id = sess.get("user_id")
    if user_id:
        ctx.pool = "web_session"
        ctx.key = f"web:user:{user_id}"
        ctx.tier = sess.get("tier", "free") or "free"
        return ctx

    # 4. Anonymous.
    ip = req.client_ip or "unknown"
    ctx.key = f"anon:{ip}"
    return ctx


# ---------------------------------------------------------------------------
# Concrete adapters — thin wrappers, no business logic.
# ---------------------------------------------------------------------------


class FlaskRequestAdapter:
    """Adapter exposing Flask's ``flask.request`` as a ``RequestLike``.

    Reads ``flask.request`` lazily inside each method so the adapter can
    be instantiated outside an active request context (tests, helpers).
    """

    def header(self, name: str) -> Optional[str]:
        from flask import request

        return request.headers.get(name)

    def query_param(self, name: str) -> Optional[str]:
        from flask import request

        return request.args.get(name)

    @property
    def client_ip(self) -> str:
        # Flask runs ProxyFix(x_for=1) which rewrites remote_addr to the
        # real client IP after stripping the trusted nginx hop.
        # Parsing the raw X-Forwarded-For header directly is spoofable
        # (an attacker can prepend a fake IP to claim a fresh anon bucket).
        # remote_addr is already the correct, trusted value after ProxyFix.
        from flask import request

        return request.remote_addr or ""


class FastAPIRequestAdapter:
    """Adapter exposing a ``starlette.requests.Request`` as a ``RequestLike``."""

    def __init__(self, request) -> None:
        self._r = request

    def header(self, name: str) -> Optional[str]:
        return self._r.headers.get(name)

    def query_param(self, name: str) -> Optional[str]:
        return self._r.query_params.get(name)

    @property
    def client_ip(self) -> str:
        # ProxyHeadersMiddleware (registered in fastapi_app/main.py) has
        # already rewritten request.client.host to the real client IP.
        # Reading the raw X-Forwarded-For header here would be spoofable.
        client = getattr(self._r, "client", None)
        return (client.host if client else "") or "unknown"


__all__ = [
    "_classify_pure",
    "FlaskRequestAdapter",
    "FastAPIRequestAdapter",
    "RequestLike",
    "SessionGetter",
]
