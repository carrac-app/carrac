"""Per-user AI chat concurrency slot — shared between Flask and FastAPI.

The Flask handler (carrac/ai/routes.py) and the FastAPI handler
(carrac/fastapi_app/routers/ai.py) both import from here so there is one
source of truth for the Redis-backed concurrency guard.

The lock prevents parallel AI chat requests from the same user (burst abuse).
Redis is used when available so the guard works across gunicorn workers.
Falls back to an in-process dict when Redis is down (single-worker correctness
only; acceptable degradation).
"""

from __future__ import annotations

import logging
import os
import threading
import time

logger = logging.getLogger("carrac.ai.chat_slot")

_active_chats: dict = {}  # user_id -> timestamp  (in-process fallback)
_active_chats_lock = threading.Lock()
_ACTIVE_CHAT_TTL = 120  # seconds

# Module-level Redis client cache for the chat slot (env-based — works without
# Flask app context, which is required for the FastAPI path).
_chat_slot_redis = None
_chat_slot_redis_url: str | None = None


def _redis_client():
    """Return a redis client for the chat-lock DB, or None if unavailable.

    Reads REDIS_URL / REDIS_SESSION_URL from env directly so this function
    works in both the Flask (app context) and FastAPI (no app context) paths.
    """
    global _chat_slot_redis, _chat_slot_redis_url
    try:
        import redis as _redis_lib

        url = os.getenv("REDIS_URL") or os.getenv("REDIS_SESSION_URL") or ""
        if not url:
            return None
        if _chat_slot_redis is not None and _chat_slot_redis_url == url:
            return _chat_slot_redis
        client = _redis_lib.from_url(url, socket_connect_timeout=2)
        # Warm verify — if Redis is down we fall back immediately rather than
        # timing out mid-request.
        client.ping()
        _chat_slot_redis = client
        _chat_slot_redis_url = url
        return client
    except Exception as e:
        logger.debug("AI chat slot: redis unavailable (%s), using in-process fallback", e)
        return None


def acquire_chat_slot(user_id: int) -> bool:
    """Atomically reserve the chat slot for *user_id*.

    Returns True when the slot was successfully acquired (caller may proceed).
    Returns False when the slot is already held by another in-flight request.
    """
    key = f"mcd:ai:chat:{user_id}"
    client = _redis_client()
    if client is not None:
        try:
            return bool(client.set(key, int(time.time()), nx=True, ex=_ACTIVE_CHAT_TTL))
        except Exception as e:
            logger.warning("AI chat slot: redis SET NX failed (%s), falling back", e)
    # In-process fallback.
    with _active_chats_lock:
        last_start = _active_chats.get(user_id)
        if last_start and (time.time() - last_start) < _ACTIVE_CHAT_TTL:
            return False
        _active_chats[user_id] = time.time()
        return True


def release_chat_slot(user_id: int) -> None:
    """Release the chat slot for *user_id* (idempotent)."""
    key = f"mcd:ai:chat:{user_id}"
    client = _redis_client()
    if client is not None:
        try:
            client.delete(key)
        except Exception:
            pass
    with _active_chats_lock:
        _active_chats.pop(user_id, None)
