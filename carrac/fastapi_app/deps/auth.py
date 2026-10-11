"""FastAPI auth dependencies — port of ``carrac/auth/decorators.py``.

Cross-cutting infra. No migrated authed endpoints used these at first;
these dependencies stand up the shape so later routes
can wire ``Annotated[User, Depends(require_login)]`` without bespoke shims.

Behavioural parity: same decorator catalogue, session contract and error
shape as the Flask side.

The Flask side stays untouched. Flask requests still flow through
``carrac/auth/decorators.py`` and ``carrac/rate_pools.py``; FastAPI
requests flow through this module. Common semantics (rate-pool classification,
api_key/widget-token resolution) live in pure helpers under
``carrac.auth.pure_resolvers`` so both runtimes share one source of truth.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Annotated, Any, Mapping, Optional

from fastapi import Depends, HTTPException, Request

from carrac.auth.pure_resolvers import (
    SHARED_KEY_USER_ID,
    resolve_widget_token,
)

logger = logging.getLogger("carrac.fastapi.auth")

# Tier sets — imported from the single source of truth (carrac/tiers.py).
from carrac.tiers import PAID_TIERS, TESTER_OR_HIGHER  # noqa: E402,F401 — re-exported for callers


# ---------------------------------------------------------------------------
# Rate context (single object on request.state.rate, see Agent B's spec).
# ---------------------------------------------------------------------------


@dataclass
class RateContext:
    """Per-request classification + identity bus.

    Mirrors the eight ``flask.g.rate_*`` / ``flask.g.api_key_*`` attrs that
    Flask's ``before_request`` chain populates today. FastAPI deps read from
    here instead of ``flask.g`` so the strangler period doesn't leak Flask
    primitives into FastAPI handlers.
    """

    pool: str = "anonymous"
    """``api_key`` | ``widget_token`` | ``web_session`` | ``anonymous``."""

    key: str = ""
    """Pool-namespaced bucket key for rate-limit counting."""

    tier: str = "free"
    """Effective tier of the caller."""

    api_key_user_id: Optional[int] = None
    """``cvx_k_`` per-user key binding. ``None`` for shared admin key
    callers (anonymous bot) and non-key requests."""

    api_key_tier: Optional[str] = None

    api_key_scope: str = "full"

    api_key_internal: bool = False
    """True excludes this key from tier metering, usage
    analytics, and the weekly usage review's LLM-spend attribution. False for every key
    except the one explicitly flagged in the DB."""

    widget_user_id: Optional[int] = None

    widget_token_id: Optional[int] = None

    widget_user_tier: Optional[str] = None

    request_id: str = ""

    extras: dict[str, Any] = field(default_factory=dict)


def _is_json_client(request: Request) -> bool:
    accept = request.headers.get("accept", "")
    ct = request.headers.get("content-type", "")
    if "application/json" in accept or "application/json" in ct:
        return True
    return request.url.path.startswith("/api/")


def _raise_redirect_to_login() -> None:
    raise HTTPException(status_code=302, headers={"Location": "/login"})


def _reject_readonly_write(scope: Optional[str], method: str) -> None:
    """Raise 403 when a read-only API key attempts a write operation.

    Mirrors the Flask ``_enforce_api_key_scope`` before_request hook in
    ``carrac/__init__.py::_enforce_api_key_scope``. Must only be called on the API-key branch
    of a dependency — session users have their own auth path and are unaffected.

    ``method`` should be ``request.method`` (already upper-cased by Starlette).
    """
    if scope == "read-only" and method not in ("GET", "HEAD", "OPTIONS"):
        raise HTTPException(
            status_code=403,
            detail="read-only API key cannot perform write operations",
        )


def _raise_auth_required(request: Request) -> None:
    if _is_json_client(request):
        raise HTTPException(status_code=401, detail="Authentication required")
    _raise_redirect_to_login()


def _session(request: Request) -> Mapping[str, Any]:
    """Return the session dict the middleware stashed (or ``{}``).

    Always safe to call: the middleware always sets ``state.session`` (even
    if empty) when the cookie is missing/invalid.
    """
    return getattr(request.state, "session", {}) or {}


# ---------------------------------------------------------------------------
# Classification — single Depends that all auth/rate deps build on.
# ---------------------------------------------------------------------------


def _classify(request: Request) -> RateContext:
    """Build a ``RateContext`` for the current request.

    Delegates to ``carrac/auth/rate_classify.py::_classify_pure``
    so the Flask and FastAPI runtimes share one algorithm. This module
    keeps the public ``classify_request`` ``Depends`` plus the
    ``request.state.rate`` cache, but the per-pool decision logic is the
    shared pure helper.
    """
    # Local import: rate_classify imports RateContext from this module, so
    # importing it at module top-level would create a cycle.
    from carrac.auth.rate_classify import (
        FastAPIRequestAdapter,
        _classify_pure,
    )

    return _classify_pure(
        FastAPIRequestAdapter(request),
        lambda: dict(_session(request)),
    )


def classify_request(request: Request) -> RateContext:
    """Public ``Depends`` for the request classification chain.

    Caches the resulting ``RateContext`` on ``request.state.rate`` so a
    single request running through several deps that all depend on
    classification only resolves once. Equivalent to Flask's
    ``before_request`` ``classify_request()`` writing to ``flask.g``.
    """
    cached: RateContext | None = getattr(request.state, "rate", None)
    if cached is not None:
        return cached
    ctx = _classify(request)
    request.state.rate = ctx
    return ctx


RateCtxDep = Annotated[RateContext, Depends(classify_request)]


# ---------------------------------------------------------------------------
# User-facing dependencies.
# ---------------------------------------------------------------------------


def current_user(request: Request, ctx: RateCtxDep) -> Optional[dict]:
    """Return the logged-in session user dict, or ``None``.

    Mirrors ``carrac/auth/decorators.py::get_current_user``. Reads only
    from session — API key and widget token paths produce a different
    identity (see ``api_key_user`` / ``widget_token_user``).
    """
    sess = _session(request)
    if "user_id" not in sess:
        return None
    # Honour the kill-epoch. _reject_killed_sessions was a
    # Flask-only before_app_request hook, so a session invalidated by
    # deactivation / password change / log-out-everywhere kept working on this
    # whole surface if the Redis blob delete did not land. Both stacks read the
    # same blob; both must apply the same rule.
    from carrac.auth.session_kill import session_is_killed

    if session_is_killed(sess):
        request.state.session = {}
        return None
    return {
        "id": sess.get("user_id"),
        "email": sess.get("email"),
        "display_name": sess.get("display_name"),
        "is_admin": bool(sess.get("is_admin", False)),
        "tier": sess.get("tier", "free"),
    }


CurrentUserDep = Annotated[Optional[dict], Depends(current_user)]


def require_login(
    request: Request,
    user: CurrentUserDep,
) -> dict:
    """Require a logged-in session user. Always 302-redirects to ``/login``.

    Matches the Flask ``@login_required`` decorator's behaviour exactly: it
    never emits JSON 401.
    Routes that need JSON 401 for unauthenticated API callers must use
    ``auth_or_key`` instead.
    """
    if user is None:
        _raise_redirect_to_login()
    return user  # type: ignore[return-value]


def require_admin(
    request: Request,
    user: CurrentUserDep,
) -> dict:
    """Require admin session with DB re-verification.

    DB re-verify is a security requirement, not an optimisation — kept
    even though it adds a query per admin call. Transient DB error → 503,
    NOT a session wipe (matches Flask behaviour).
    """
    if user is None:
        _raise_redirect_to_login()
    assert user is not None  # for type-checker
    if not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="Admin access required")
    try:
        from user_db import user_db  # local import: heavy, optional in tests
        record = user_db.get_user_by_id(user["id"])
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[SECURITY] Admin DB re-verification failed — denying access: %s", exc
        )
        raise HTTPException(
            status_code=503, detail="Admin panel temporarily unavailable"
        )
    if not record or not record.get("is_admin") or not record.get("is_active", True):
        # Best-effort session invalidation in a stateless dep.
        request.state.session = {}
        _raise_redirect_to_login()
    return user


def require_tier(*allowed_tiers: str):
    """Factory: returns a ``Depends`` that enforces one of ``allowed_tiers``.

    Usage::

        @router.get("/scan")
        def scan(s: Annotated[dict, Depends(require_tier("tester", "admin"))]):
            ...
    """
    allowed = frozenset(allowed_tiers)

    def _dep(request: Request, user: CurrentUserDep) -> dict:
        if user is None:
            _raise_auth_required(request)
        assert user is not None
        tier = user.get("tier", "free") or "free"
        if tier not in allowed:
            if _is_json_client(request):
                raise HTTPException(
                    status_code=403,
                    detail={
                        "error": "Subscription required",
                        "upgrade_required": True,
                        "current_tier": tier,
                        "required_tiers": list(allowed_tiers),
                    },
                )
            raise HTTPException(
                status_code=302, headers={"Location": "/"}
            )
        return user

    return _dep


require_premium = require_tier("member", "tester", "premium", "admin")


def auth_or_key(
    request: Request,
    ctx: RateCtxDep,
    user: CurrentUserDep,
) -> dict:
    """Allow access via session login OR API key.

    Matches ``@auth_or_key_required``: 400 on URL-param ``?key=``,
    pass-through on session OR valid API key, 401 JSON or 302 otherwise.
    """
    # 1. URL-param API key — explicit deprecation, not silently dropped.
    if request.query_params.get("key"):
        logger.warning(
            "[SECURITY] Rejected: API key passed as URL parameter on %s",
            request.url.path,
        )
        raise HTTPException(
            status_code=400,
            detail="API key via URL parameter is deprecated. Use X-API-Key header.",
        )

    # 2. Session.
    if user is not None:
        return {
            "source": "session",
            "user": user,
            "tier": user.get("tier", "free"),
        }

    # 3. API key (already classified by classify_request).
    if ctx.pool == "api_key":
        _reject_readonly_write(ctx.api_key_scope, request.method)
        return {
            "source": "api_key",
            "user_id": ctx.api_key_user_id,  # may be None for shared admin key
            "tier": ctx.tier,
            "scope": ctx.api_key_scope,
        }

    _raise_auth_required(request)
    raise AssertionError("unreachable")  # for type-checker


def tester_or_key(
    request: Request,
    ctx: RateCtxDep,
    user: CurrentUserDep,
) -> dict:
    """Allow valid API key (any tier) OR session with tester+ tier.

    Replicates ``@tester_or_key_required``: API key check happens FIRST and
    bypasses the tier requirement entirely (shared admin key + per-user keys
    both pass without tier check).
    """
    if ctx.pool == "api_key":
        _reject_readonly_write(ctx.api_key_scope, request.method)
        return {
            "source": "api_key",
            "user_id": ctx.api_key_user_id,
            "tier": ctx.tier,
            "scope": ctx.api_key_scope,
        }
    if user is not None:
        tier = user.get("tier", "free") or "free"
        if tier in TESTER_OR_HIGHER:
            return {"source": "session", "user": user, "tier": tier}
        raise HTTPException(
            status_code=403,
            detail={"error": "Premium access required", "upgrade_required": True},
        )
    _raise_auth_required(request)
    raise AssertionError("unreachable")


def api_key_user(
    request: Request,
    ctx: RateCtxDep,
) -> dict:
    """Require a valid API key. 401 JSON otherwise.

    Returns ``{user_id, tier, scope}`` — ``user_id`` is ``None`` for shared
    admin key callers (anonymous bot, no user row). Callers that scope data
    to a user must handle the ``None`` case.
    """
    if request.query_params.get("key"):
        raise HTTPException(
            status_code=400,
            detail="API key via URL parameter is deprecated. Use X-API-Key header.",
        )
    if ctx.pool != "api_key":
        raise HTTPException(status_code=401, detail="Authentication required")
    _reject_readonly_write(ctx.api_key_scope, request.method)
    return {
        "user_id": ctx.api_key_user_id,
        "tier": ctx.tier,
        "scope": ctx.api_key_scope,
    }


def widget_token_user(request: Request) -> dict:
    """Require a valid widget token (``cvx_w_<32hex>``). 401/429 otherwise.

    Token source: ``Authorization: Bearer ...`` header OR ``?token=`` query
    param. Mirrors ``carrac/widget/auth.py::widget_token_required``,
    inlined because the Flask helper reads ``flask.request`` directly and
    can't be reused from a FastAPI context.
    """
    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        token = auth_header[7:].strip()
    else:
        token = request.query_params.get("token", "").strip()

    info = resolve_widget_token(token)
    if info is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    if info.get("rate_limited"):
        raise HTTPException(
            status_code=429,
            detail={"error": "Rate limit exceeded", "retry_after": 60},
            headers={"Retry-After": "60"},
        )

    request.state.widget_user_id = info["user_id"]
    request.state.widget_token_id = info["token_id"]
    request.state.widget_user_tier = info.get("tier", "free")
    return {
        "user_id": info["user_id"],
        "token_id": info["token_id"],
        "tier": info.get("tier", "free"),
    }


# ---------------------------------------------------------------------------
# Helpers (Flask helper parity — used internally by deps + ports).
# ---------------------------------------------------------------------------


def get_request_tier(request: Request) -> str:
    """Tier resolution for the current request — port of
    ``carrac/auth/decorators.py::get_request_tier``.
    """
    sess = _session(request)
    if sess.get("user_id"):
        return sess.get("tier", "free") or "free"
    ctx: RateContext | None = getattr(request.state, "rate", None)
    if ctx is None:
        ctx = classify_request(request)
    if ctx.tier:
        return ctx.tier
    return "free"


__all__ = [
    "RateContext",
    "classify_request",
    "current_user",
    "require_login",
    "require_admin",
    "require_tier",
    "require_premium",
    "auth_or_key",
    "tester_or_key",
    "api_key_user",
    "widget_token_user",
    "get_request_tier",
    "SHARED_KEY_USER_ID",
]
