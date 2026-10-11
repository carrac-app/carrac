"""CarracCSRFMiddleware — raw ASGI port of flask-wtf CSRFProtect.

Mirrors ``flask_wtf.csrf.validate_csrf`` without a Flask app context:

* Wire token format: ``itsdangerous.URLSafeTimedSerializer(SECRET_KEY,
  salt="wtf-csrf-token").dumps(session["csrf_token"])`` — the salt is
  hard-coded inside flask-wtf and must match exactly.
* Validation: unsign with ``max_age=3600`` (matches ``WTF_CSRF_TIME_LIMIT``
  in ``security.py``), then ``hmac.compare_digest`` against the raw token
  the Flask side stored in ``session["csrf_token"]``.

Bypass rules:

1. Skip on safe methods (GET/HEAD/OPTIONS/TRACE).
2. Skip on the explicit allowlist of paths that are ``csrf.exempt`` in
   Flask and have no manual ``validate_csrf`` (drawings, journal, prefs,
   AI, sync, scanner, MCP, formulas, quant, history, …). Paths are matched
   as the router sees them, so ``/api/v2/<path>`` (the second mount in
   ``carrac/asgi.py``) gets exactly the rules ``/<path>`` gets.
3. Skip when an ``X-API-Key`` header is present — API-key callers don't
   carry session cookies, so they have nothing for CSRF to validate
   against. The one exception is ``/api/widget/tokens``: those writes
   have no API-key bypass and are always checked against the session.
4. Skip on anonymous callers (no ``session["user_id"]``). The auth Depends on the route
   itself rejects unauthenticated writes; CSRF only adds value once the
   caller is authenticated via session.

Order: register AFTER ``ResponseHeadersMiddleware`` and BEFORE
``CarracSessionMiddleware`` in ``main.py``. ASGI middleware is LIFO at
request time — the LAST ``add_middleware`` call is the outermost wrapper.
We need ``CarracSessionMiddleware`` to populate ``scope["state"].session``
*before* this middleware reads it, which means session must be the OUTER
layer (added later), and CSRF must be added FIRST among the two.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
from typing import Mapping

import itsdangerous
from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger("carrac.fastapi.csrf")

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})

# CSRF salt — flask-wtf hard-codes "wtf-csrf-token" (flask_wtf.csrf). Must
# match exactly so a Flask-issued token validates here and vice versa.
_SALT = "wtf-csrf-token"

# Token TTL — IMPORTED, not restated. A literal here that merely claims to
# match security.py can drift: if the token lifetime were shorter than the
# 30-day session, a tab open past it would have every write rejected with 403
# while GETs kept succeeding.
#
# A comment asserting two numbers agree is not a mechanism for keeping them
# agreeing. Reading the value is. tests/test_csrf_lifetime.py fails if a second
# source of truth is introduced.
try:
    from security import CSRF_TIME_LIMIT_SECONDS as _MAX_AGE_SECONDS
except Exception:  # pragma: no cover - security.py must import cleanly in production
    # Fail SAFE-for-the-user, not safe-for-the-purist: a broken import here
    # would otherwise silently restore a 1-hour token lifetime.
    _MAX_AGE_SECONDS = 24 * 30 * 3600

# Paths where Flask does not enforce CSRF today. This middleware mirrors that —
# strengthening would break clients that have never sent
# a CSRF token to these endpoints. Prefix-matched.
_SKIP_PREFIXES: tuple[str, ...] = (
    # Blueprint-exempt with no manual CSRF (Flask side enforces nothing):
    "/api/drawings",
    "/api/journal",
    "/api/preferences",
    "/api/settings",
    "/api/ai",
    "/api/sync",
    "/api/scanner",
    "/api/mcp",
    "/mcp",  # MCP JSON-RPC endpoint, API key auth, no CSRF token.
    "/api/formulas",
    "/api/quant",
    "/api/usage",
    "/api/screenshots",
    "/api/chart",  # GET-only chart data and chart-internal both fall here
    "/api/history",
    "/api/custom-formulas",
    "/api/strategies",
    # GET-only routes (already FastAPI):
    "/api/batch",
    "/api/positions",
    "/api/status",
    "/api/relative-strength",
    "/api/asset/",
    "/api/assets",
    # reports_bp — engine endpoints are individually @csrf.exempt;
    # the rest are GET-only. Prefix-skip is safe.
    "/api/report",
    "/api/engine",
    # Per-route @csrf.exempt:
    "/api/billing/paddle-webhook",
    "/api/heartbeat",
    "/api/csp-report",
    "/api/error-tunnel",
)

_403_BODY = json.dumps({"error": "CSRF token missing or invalid"}).encode("utf-8")
_403_HEADERS = (
    (b"content-type", b"application/json"),
    (b"content-length", str(len(_403_BODY)).encode("ascii")),
)


class CarracCSRFMiddleware:
    """Raw ASGI middleware enforcing CSRF on session-authenticated writes."""

    def __init__(self, app: ASGIApp, secret_key: str | None = None) -> None:
        self.app = app
        key = secret_key or os.getenv("SECRET_KEY", "")
        if not key:
            # Same defensive shape Flask startup takes — log loudly, fall
            # back to a placeholder that will fail every signature check.
            # The right fix is to set SECRET_KEY in the env; until then,
            # the middleware refuses every CSRF-eligible write.
            logger.warning(
                "CarracCSRFMiddleware: SECRET_KEY env is empty — every "
                "session-authenticated write will fail CSRF validation."
            )
        self._secret_key = key or "csrf-middleware-missing-secret"

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "GET").upper()
        if method in _SAFE_METHODS:
            await self.app(scope, receive, send)
            return

        path = _route_path(scope)
        # NB: order matters — exempt blueprint prefixes are checked BEFORE
        # the X-API-Key bypass, so Flask-side exempt routes stay exempt
        # for both browser (no header) and API (with header) callers.
        for prefix in _SKIP_PREFIXES:
            if path.startswith(prefix):
                await self.app(scope, receive, send)
                return

        # Build a case-insensitive header dict once. ASGI headers are
        # tuples of bytes; lower-case lookup avoids hostname-style
        # casing surprises.
        hdrs: dict[bytes, bytes] = {}
        for name, value in scope.get("headers", ()):
            hdrs[name.lower()] = value

        api_key = hdrs.get(b"x-api-key", b"").decode("utf-8", errors="replace")
        # Widget token writes are session-only: no X-API-Key bypass.
        widget_path = path.startswith("/api/widget/tokens")
        if api_key and not widget_path:
            await self.app(scope, receive, send)
            return

        # Pull the session dict the session middleware stashed on
        # scope["state"]. State is a starlette.datastructures.State; if
        # the session middleware ran successfully, ``state.session`` is a
        # dict (possibly empty for anonymous callers). We never raise on
        # missing state — silently treat as anonymous and skip.
        session = _read_session(scope)
        if not session.get("user_id"):
            # Anonymous request. The auth Depends on the
            # route itself returns 401/302; CSRF has nothing to validate.
            await self.app(scope, receive, send)
            return

        submitted = (
            hdrs.get(b"x-csrftoken", b"").decode("utf-8", errors="replace")
            or hdrs.get(b"x-csrf-token", b"").decode("utf-8", errors="replace")
        )
        session_tok = session.get("csrf_token")

        if (
            not submitted
            or not session_tok
            or not _validate(submitted, str(session_tok), self._secret_key)
        ):
            await _send_403(send)
            return

        await self.app(scope, receive, send)


def _route_path(scope: Scope) -> str:
    """The path as the FastAPI router sees it.

    ``carrac/asgi.py`` mounts this app both at the root and at ``/api/v2``.
    Under the mount, Starlette keeps the full URL in ``scope["path"]`` and
    records the mount prefix in ``scope["root_path"]``, so
    ``/api/v2/api/watchlist/add`` reaches the same handler as
    ``/api/watchlist/add``. The skip list and the widget rule must judge
    both spellings the same way, so strip the prefix before matching.
    """
    path: str = scope.get("path", "")
    root: str = scope.get("root_path", "")
    if root and path.startswith(root) and path[len(root):len(root) + 1] in ("/", ""):
        return path[len(root):] or "/"
    return path


def _read_session(scope: Scope) -> Mapping[str, object]:
    """Return the session dict the upstream session middleware stashed.

    ``CarracSessionMiddleware`` always sets ``scope["state"].session``
    (an empty dict for anonymous / cookie-less requests). If the state
    object is missing entirely (middleware not installed in tests etc.),
    return an empty mapping so we degrade to "no session, skip CSRF".
    """
    state = scope.get("state")
    if state is None:
        return {}
    sess = getattr(state, "session", None)
    if isinstance(sess, dict):
        return sess
    if isinstance(state, dict):
        candidate = state.get("session")
        if isinstance(candidate, dict):
            return candidate
    return {}


def _validate(submitted: str, session_token: str, secret_key: str) -> bool:
    """Replicate ``flask_wtf.csrf.validate_csrf`` outside Flask context."""
    try:
        signer = itsdangerous.URLSafeTimedSerializer(secret_key, salt=_SALT)
        unsigned = signer.loads(submitted, max_age=_MAX_AGE_SECONDS)
    except (itsdangerous.SignatureExpired, itsdangerous.BadData):
        return False
    return hmac.compare_digest(session_token, str(unsigned))


async def _send_403(send: Send) -> None:
    start: Message = {
        "type": "http.response.start",
        "status": 403,
        "headers": [list(h) for h in _403_HEADERS],
    }
    body: Message = {
        "type": "http.response.body",
        "body": _403_BODY,
        "more_body": False,
    }
    await send(start)
    await send(body)


__all__ = ["CarracCSRFMiddleware"]
