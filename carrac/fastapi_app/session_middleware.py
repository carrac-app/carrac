"""Hand-rolled Redis session middleware for the FastAPI sub-app.

Reads the SAME server-side session keys Flask-Session writes today
(``mcd:session:<sid>`` in Redis DB 1, msgpack-encoded). Starlette's
stock ``SessionMiddleware`` is signed-cookie only — it cannot read a
Redis-backed session and would log every user out the moment a route
flipped to FastAPI.

Invariant the middleware preserves so the cross-runtime test passes:

* Cookie name: ``"session"`` (Flask default; not overridden).
* Cookie value: raw SID (43-char base64url ``secrets.token_urlsafe(32)``);
  no signing — Flask-Session ``SESSION_USE_SIGNER=False`` default.
* Redis key: ``mcd:session:<sid>`` in DB 1.
* Codec: ``msgspec.msgpack`` (Flask-Session 0.8.0 default).
* TTL: 30 days; refreshed each request when an active session is
  present (Flask uses ``SESSION_REFRESH_EACH_REQUEST=True``).

Consumption is read-only: the middleware decodes the dict
into ``request.state.session`` and refreshes the Redis TTL. Write-back
(Flask's ``save_session`` equivalent) is deferred until a route
mutates the session — the only FastAPI
authed route, ``/api/v2/_whoami``, is read-only.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Mapping, MutableMapping

import msgspec.msgpack
import redis as redis_lib
from starlette.datastructures import State
from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger("carrac.fastapi.session")

COOKIE_NAME = "session"
KEY_PREFIX = "mcd:session:"
SESSION_TTL_SECONDS = 86400 * 30  # 30 days, matches PERMANENT_SESSION_LIFETIME

_DECODER = msgspec.msgpack.Decoder()


def _build_redis_client(redis_url: str | None) -> redis_lib.Redis | None:
    """Build a Redis client from REDIS_SESSION_URL with REDIS_URL fallback.

    Returns ``None`` when neither is set (test environments without Redis);
    the middleware then yields an empty session for every request, matching
    Flask-Session's filesystem-fallback behaviour for unauthenticated tests.
    """
    if not redis_url:
        return None
    try:
        return redis_lib.from_url(redis_url)
    except Exception as exc:  # noqa: BLE001 — surface but don't crash startup
        logger.warning("CarracSessionMiddleware: Redis init failed: %s", exc)
        return None


def _parse_cookie_header(raw: bytes) -> str | None:
    """Extract the ``session=...`` value from a raw Cookie header.

    Avoids the cost of the full ``http.cookies.SimpleCookie`` parser for
    a single fixed name — the middleware is on the hot path.
    """
    try:
        text = raw.decode("latin-1")
    except Exception:  # noqa: BLE001
        return None
    for part in text.split(";"):
        if "=" not in part:
            continue
        name, _, value = part.strip().partition("=")
        if name == COOKIE_NAME:
            return value or None
    return None


class CarracSessionMiddleware:
    """Raw ASGI middleware mirroring Flask-Session's read path."""

    def __init__(self, app: ASGIApp, redis_url: str | None = None) -> None:
        self.app = app
        if redis_url is None:
            redis_url = os.getenv("REDIS_SESSION_URL") or os.getenv("REDIS_URL")
        self._redis = _build_redis_client(redis_url)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        sid, session_data = await self._load_session(scope)
        # Stash on the ASGI scope's ``state`` so FastAPI's ``request.state``
        # exposes the session as ``request.state.session``. Starlette's
        # ``HTTPConnection.state`` casts ``scope["state"]`` to ``State``, so
        # we always materialise one here — uvicorn populates ``scope["state"]``
        # as a plain dict from the lifespan state, and the dict→State cast
        # would break attribute access if a route reaches into ``request.state``
        # before Starlette wraps it.
        existing = scope.get("state")
        if isinstance(existing, State):
            existing.session = session_data
        elif isinstance(existing, dict):
            # Preserve any keys lifespan populated, then promote to State.
            existing["session"] = session_data
            scope["state"] = State(existing)
        else:
            scope["state"] = State({"session": session_data})

        await self.app(scope, receive, send)

        # Refresh TTL for active sessions, mimicking
        # SESSION_REFRESH_EACH_REQUEST=True. Read-only: never mutate the
        # value here — these routes don't write the session.
        if sid and session_data.get("user_id") and self._redis is not None:
            try:
                await asyncio.to_thread(
                    self._redis.expire, f"{KEY_PREFIX}{sid}", SESSION_TTL_SECONDS
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("session TTL refresh failed for sid=%s: %s", sid[:8], exc)

        # Mirror the Flask ``_activity_heartbeat_hook`` so
        # FastAPI-routed traffic (the SPA data plane — plays/alerts/journal/
        # scanner/AI, see the MIGRATED_* tables in carrac/asgi.py) also keeps
        # cLastActive / users.last_active fresh. ``on_activity`` throttles
        # itself (Redis SET NX, 6h) and defers the Espo/user_db/PostHog writes
        # to its own executor; both planes run in one process, so calling the
        # same function shares one throttle — no double writes.
        user_id = session_data.get("user_id")
        email = session_data.get("email")
        if user_id and email:
            try:
                from carrac.integrations import on_activity

                # The throttle check is a blocking Redis round-trip — keep it
                # off the event loop, same as the TTL refresh above.
                await asyncio.to_thread(on_activity, user_id, email)
            except Exception as exc:  # noqa: BLE001
                logger.debug("activity heartbeat dispatch failed: %s", exc)

    async def _load_session(self, scope: Scope) -> tuple[str | None, MutableMapping[str, Any]]:
        """Async session loader — Redis GET is dispatched via asyncio.to_thread
        so it never blocks the anyio event thread."""
        if self._redis is None:
            return None, {}
        sid: str | None = None
        for name, value in scope.get("headers", ()):
            if name.lower() == b"cookie":
                sid = _parse_cookie_header(value)
                if sid:
                    break
        if not sid:
            return None, {}
        try:
            raw = await asyncio.to_thread(self._redis.get, f"{KEY_PREFIX}{sid}")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Redis GET failed for session %s: %s", sid[:8], exc)
            return sid, {}
        if not raw:
            return sid, {}
        try:
            data = _DECODER.decode(raw)
            if isinstance(data, Mapping):
                # Promote to a mutable dict so callers can stash debug fields
                # without triggering msgspec immutability surprises.
                return sid, dict(data)
            return sid, {}
        except Exception as exc:  # noqa: BLE001
            # Expired/corrupt blobs are treated as no session (Flask-Session
            # behaviour). Don't 500 the request just because a cookie went
            # stale — the next login overwrites the key.
            logger.debug("session decode failed for sid=%s: %s", sid[:8], exc)
            return sid, {}
